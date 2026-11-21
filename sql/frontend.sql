-- Frontend access layer (splitting-plan.md, milestone 0). Needs Supabase (the auth schema). Safe to rerun.
-- The Python backend connects as the table owner and is not affected by anything below.
-- The web app uses the public anon key, so protection is row-level security (RLS) and revoked grants, nothing else.

-- ===== 1. Close everything for the public API roles; open only what is granted at the end of this file =====
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC, anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC, anon, authenticated;

-- RLS was already on for hotels, reviews and scrape_runs (no policies = no access for the web app). Switch it on everywhere.
ALTER TABLE codebook_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE llm_phrase_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE pilot_sample ENABLE ROW LEVEL SECURITY;
ALTER TABLE prices ENABLE ROW LEVEL SECURITY;
ALTER TABLE prompt_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE review_windows ENABLE ROW LEVEL SECURITY;
ALTER TABLE validation_split ENABLE ROW LEVEL SECURITY;
ALTER VIEW hotels_v SET (security_invoker = true);   -- a view otherwise runs with its owner's rights and would bypass RLS

-- ===== 2. Users =====
CREATE TABLE IF NOT EXISTS profiles (
  user_id uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  role text NOT NULL CHECK (role IN ('lead','researcher','expert','coder')),
  display_name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION app_role() RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS
$$ SELECT role FROM public.profiles WHERE user_id = auth.uid() $$;

-- ===== 3. Which sets coders may work on. The lead opens them; the test sets stay closed until the codebook is frozen =====
CREATE TABLE IF NOT EXISTS set_access (
  set_name text PRIMARY KEY CHECK (set_name IN ('training','dev','test')),
  is_open boolean NOT NULL DEFAULT false,
  changed_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO set_access (set_name) VALUES ('training'), ('dev'), ('test') ON CONFLICT DO NOTHING;
ALTER TABLE set_access ENABLE ROW LEVEL SECURITY;

-- training = is_training; dev = dev_set + supp_rel_dev; test = test_set + supp_rel_test. Pilot reviews belong to no set (done on paper).
CREATE OR REPLACE FUNCTION review_set(p_training boolean, p_gold text) RETURNS text LANGUAGE sql IMMUTABLE SET search_path = '' AS
$$ SELECT CASE WHEN p_training THEN 'training'
               WHEN p_gold IN ('dev_set','supp_rel_dev') THEN 'dev'
               WHEN p_gold IN ('test_set','supp_rel_test') THEN 'test' END $$;

-- A closed round is an earlier development round: coders no longer see it, staff still do.
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS dev_round integer;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS round_closed boolean NOT NULL DEFAULT false;

-- ===== 4. Coders' splits (offsets: Unicode code points, half-open [start, end)) =====
CREATE TABLE IF NOT EXISTS human_splits (
  split_id bigserial PRIMARY KEY,
  review_id bigint NOT NULL REFERENCES reviews(review_id) ON DELETE RESTRICT,
  coder uuid NOT NULL REFERENCES profiles(user_id) ON DELETE RESTRICT,
  start_offset integer NOT NULL CHECK (start_offset >= 0),
  end_offset integer NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (end_offset > start_offset)
);
CREATE INDEX IF NOT EXISTS human_splits_review_coder_idx ON human_splits (review_id, coder);
ALTER TABLE human_splits ENABLE ROW LEVEL SECURITY;

-- Pressing "Finish set" writes one row per review (via finish_set only). From then on the coder's splits for it are locked.
CREATE TABLE IF NOT EXISTS split_submissions (
  review_id bigint NOT NULL REFERENCES reviews(review_id) ON DELETE RESTRICT,
  coder uuid NOT NULL REFERENCES profiles(user_id) ON DELETE RESTRICT,
  submitted_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (review_id, coder)
);
ALTER TABLE split_submissions ENABLE ROW LEVEL SECURITY;

-- Filled later by adjudication (lead).
CREATE TABLE IF NOT EXISTS agreed_phrases (
  phrase_id bigserial PRIMARY KEY,
  review_id bigint NOT NULL REFERENCES reviews(review_id) ON DELETE RESTRICT,
  start_offset integer NOT NULL CHECK (start_offset >= 0),
  end_offset integer NOT NULL,
  phrase_text text NOT NULL,
  CHECK (end_offset > start_offset)
);
CREATE INDEX IF NOT EXISTS agreed_phrases_review_idx ON agreed_phrases (review_id);
ALTER TABLE agreed_phrases ENABLE ROW LEVEL SECURITY;

-- True if the signed-in coder may add/change/delete splits of this review (set open, not yet finished).
CREATE OR REPLACE FUNCTION can_edit_split(p_review bigint) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS
$$ SELECT public.app_role() = 'coder'
      AND NOT EXISTS (SELECT 1 FROM public.split_submissions s WHERE s.review_id = p_review AND s.coder = auth.uid())
      AND EXISTS (SELECT 1 FROM public.reviews r JOIN public.set_access a ON a.set_name = public.review_set(r.is_training, r.gold_set)
                  WHERE r.review_id = p_review AND a.is_open AND NOT r.round_closed) $$;

-- Row checks: inside the text, no leading/trailing whitespace, no overlap with the same coder's other phrases of the review.
CREATE OR REPLACE FUNCTION human_splits_check() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE t text;
BEGIN
  SELECT r.text INTO t FROM public.reviews r WHERE r.review_id = NEW.review_id;
  IF t IS NULL THEN RAISE EXCEPTION 'unknown review %', NEW.review_id; END IF;
  IF NEW.end_offset > char_length(t) THEN RAISE EXCEPTION 'phrase ends after the end of the review'; END IF;
  IF substr(t, NEW.start_offset + 1, NEW.end_offset - NEW.start_offset) ~ '^\s|\s$' THEN
    RAISE EXCEPTION 'phrase must not start or end with whitespace';
  END IF;
  IF EXISTS (SELECT 1 FROM public.human_splits h WHERE h.review_id = NEW.review_id AND h.coder = NEW.coder
             AND h.split_id <> NEW.split_id AND h.start_offset < NEW.end_offset AND h.end_offset > NEW.start_offset) THEN
    RAISE EXCEPTION 'phrases must not overlap';
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS human_splits_check_trg ON human_splits;
CREATE TRIGGER human_splits_check_trg BEFORE INSERT OR UPDATE ON human_splits FOR EACH ROW EXECUTE FUNCTION human_splits_check();

-- Every non-space character covered, no gaps (overlaps are blocked by the trigger).
CREATE OR REPLACE FUNCTION split_complete(p_review bigint, p_coder uuid) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
DECLARE t text; prev integer := 0; n integer := 0; r record;
BEGIN
  SELECT x.text INTO t FROM public.reviews x WHERE x.review_id = p_review;
  FOR r IN SELECT h.start_offset s, h.end_offset e FROM public.human_splits h WHERE h.review_id = p_review AND h.coder = p_coder ORDER BY h.start_offset LOOP
    n := n + 1;
    IF substr(t, prev + 1, r.s - prev) ~ '\S' THEN RETURN false; END IF;
    prev := r.e;
  END LOOP;
  RETURN n > 0 AND substr(t, prev + 1) !~ '\S';
END $$;

-- Small partial indexes: the set predicates below must stay plain column tests so Postgres can use them (a function call per row timed out).
CREATE INDEX IF NOT EXISTS reviews_training_idx ON reviews (review_id) WHERE is_training;
CREATE INDEX IF NOT EXISTS reviews_gold_idx ON reviews (review_id) WHERE gold_set IS NOT NULL;

-- ===== 5. Functions the web app calls =====
-- Coder queue: review_id, language and text only (no title, rating, hotel). Same fixed order for every coder.
CREATE OR REPLACE FUNCTION coder_queue(p_set text) RETURNS TABLE (review_id bigint, lang text, body text, pos integer, finished boolean)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
BEGIN
  IF public.app_role() <> 'coder' THEN RAISE EXCEPTION 'coders only'; END IF;
  IF NOT EXISTS (SELECT 1 FROM public.set_access a WHERE a.set_name = p_set AND a.is_open) THEN RAISE EXCEPTION 'set % is not open', p_set; END IF;
  RETURN QUERY
    SELECT r.review_id, r.language, r.text, (row_number() OVER (ORDER BY md5(r.review_id::text)))::integer,
           EXISTS (SELECT 1 FROM public.split_submissions s WHERE s.review_id = r.review_id AND s.coder = auth.uid())
    FROM public.reviews r WHERE r.is_selected AND ((p_set = 'training' AND r.is_training) OR (p_set = 'dev' AND r.gold_set IN ('dev_set','supp_rel_dev') AND NOT r.round_closed)
                                OR (p_set = 'test' AND r.gold_set IN ('test_set','supp_rel_test')))
    ORDER BY md5(r.review_id::text);
END $$;

-- Autosave: replace the signed-in coder's phrases of one review in one step (all or nothing). p_phrases = [{"start":0,"end":15}, ...].
-- The row trigger still checks bounds, whitespace and overlap. Partial coverage is allowed here; finish_set demands full coverage.
CREATE OR REPLACE FUNCTION save_splits(p_review bigint, p_phrases jsonb) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
BEGIN
  IF NOT public.can_edit_split(p_review) THEN RAISE EXCEPTION 'this review cannot be edited'; END IF;
  DELETE FROM public.human_splits WHERE review_id = p_review AND coder = auth.uid();
  INSERT INTO public.human_splits (review_id, coder, start_offset, end_offset)
    SELECT p_review, auth.uid(), (x->>'start')::integer, (x->>'end')::integer FROM jsonb_array_elements(p_phrases) x;
END $$;

-- Finish set: all reviews of the set must be fully covered; then they are locked.
CREATE OR REPLACE FUNCTION finish_set(p_set text) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE n integer; bad integer;
BEGIN
  IF public.app_role() <> 'coder' THEN RAISE EXCEPTION 'coders only'; END IF;
  IF NOT EXISTS (SELECT 1 FROM public.set_access a WHERE a.set_name = p_set AND a.is_open) THEN RAISE EXCEPTION 'set % is not open', p_set; END IF;
  SELECT count(*) FILTER (WHERE NOT public.split_complete(r.review_id, auth.uid())), count(*) INTO bad, n
  FROM public.reviews r WHERE r.is_selected AND ((p_set = 'training' AND r.is_training) OR (p_set = 'dev' AND r.gold_set IN ('dev_set','supp_rel_dev') AND NOT r.round_closed)
                                OR (p_set = 'test' AND r.gold_set IN ('test_set','supp_rel_test')))
    AND NOT EXISTS (SELECT 1 FROM public.split_submissions s WHERE s.review_id = r.review_id AND s.coder = auth.uid());
  IF bad > 0 THEN RAISE EXCEPTION '% review(s) are not fully split yet', bad; END IF;
  INSERT INTO public.split_submissions (review_id, coder)
    SELECT r.review_id, auth.uid() FROM public.reviews r WHERE r.is_selected AND ((p_set = 'training' AND r.is_training) OR (p_set = 'dev' AND r.gold_set IN ('dev_set','supp_rel_dev') AND NOT r.round_closed)
                                OR (p_set = 'test' AND r.gold_set IN ('test_set','supp_rel_test')))
    ON CONFLICT DO NOTHING;
  RETURN n;
END $$;

-- Lead only: open/close a set for coders, unlock a coder's finished set.
CREATE OR REPLACE FUNCTION set_set_open(p_set text, p_open boolean) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
BEGIN
  IF public.app_role() <> 'lead' THEN RAISE EXCEPTION 'lead only'; END IF;
  UPDATE public.set_access SET is_open = p_open, changed_at = now() WHERE set_name = p_set;
END $$;

CREATE OR REPLACE FUNCTION unlock_set(p_coder uuid, p_set text) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE n integer;
BEGIN
  IF public.app_role() <> 'lead' THEN RAISE EXCEPTION 'lead only'; END IF;
  DELETE FROM public.split_submissions s USING public.reviews r
   WHERE s.review_id = r.review_id AND s.coder = p_coder AND ((p_set = 'training' AND r.is_training) OR (p_set = 'dev' AND r.gold_set IN ('dev_set','supp_rel_dev'))
                                OR (p_set = 'test' AND r.gold_set IN ('test_set','supp_rel_test')));
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

-- Lead/researcher: review text and set per review (no rating, hotel or title) for the stats and adjudication pages.
DROP FUNCTION IF EXISTS review_meta();
CREATE OR REPLACE FUNCTION review_meta() RETURNS TABLE (review_id bigint, lang text, set_name text, is_supp boolean, body text, dev_round integer)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
BEGIN
  IF public.app_role() NOT IN ('lead','researcher') THEN RAISE EXCEPTION 'staff only'; END IF;
  RETURN QUERY SELECT r.review_id, r.language, public.review_set(r.is_training, r.gold_set), coalesce(r.gold_set IN ('supp_rel_dev','supp_rel_test'), false), r.text, r.dev_round
               FROM public.reviews r WHERE r.is_training OR r.gold_set IS NOT NULL;   -- no is_selected test: lets Postgres use the two partial indexes
END $$;

-- ===== 7b. Codebook versions: every adjudication records the version it was decided under =====
ALTER TABLE codebook_versions ADD COLUMN IF NOT EXISTS body text;
ALTER TABLE codebook_versions ADD COLUMN IF NOT EXISTS uploaded_by uuid;
ALTER TABLE codebook_versions ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();
INSERT INTO codebook_versions (version, created_on, change_log) VALUES ('0.2', current_date, 'Draft with the splitting rules, as shown in the app. Decisions so far are logged under this version.') ON CONFLICT DO NOTHING;
CREATE OR REPLACE FUNCTION current_codebook_version() RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS
$$ SELECT version FROM public.codebook_versions ORDER BY created_at DESC, version DESC LIMIT 1 $$;

-- ===== 8. Adjudication of splits (lead). Result: agreed_phrases + an immutable log (OSF: both versions, decision, reason) =====
CREATE TABLE IF NOT EXISTS adjudication_log (
  log_id bigserial PRIMARY KEY,
  review_id bigint NOT NULL REFERENCES reviews(review_id) ON DELETE RESTRICT,
  coder_a uuid NOT NULL REFERENCES profiles(user_id) ON DELETE RESTRICT,
  coder_b uuid NOT NULL REFERENCES profiles(user_id) ON DELETE RESTRICT,
  split_a jsonb NOT NULL,     -- coder A's original phrases [{"start":0,"end":15},...] copied from human_splits
  split_b jsonb NOT NULL,
  final_split jsonb NOT NULL, -- the agreed phrases
  reason text NOT NULL CHECK (btrim(reason) <> ''),
  decided_by uuid NOT NULL REFERENCES profiles(user_id) ON DELETE RESTRICT,
  decided_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE adjudication_log ADD COLUMN IF NOT EXISTS codebook_gap boolean NOT NULL DEFAULT false;   -- the codebook did not settle this case
ALTER TABLE adjudication_log ADD COLUMN IF NOT EXISTS codebook_version text;
CREATE INDEX IF NOT EXISTS adjudication_log_review_idx ON adjudication_log (review_id);
ALTER TABLE adjudication_log ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS adjudication_log_select ON adjudication_log;
CREATE POLICY adjudication_log_select ON adjudication_log FOR SELECT TO authenticated USING (public.app_role() = 'lead');   -- adjudication is the lead's

-- Save the agreed split of one review. Both coders must have finished it. The two original splits are copied from human_splits
-- here (not taken from the browser), so the log cannot be edited from the page. Re-deciding a review adds a new log row.
DROP FUNCTION IF EXISTS save_adjudication(bigint, uuid, uuid, jsonb, text);
CREATE OR REPLACE FUNCTION save_adjudication(p_review bigint, p_coder_a uuid, p_coder_b uuid, p_final jsonb, p_reason text, p_gap boolean DEFAULT false)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE t text; prev integer := 0; n integer := 0; r record; sa jsonb; sb jsonb;
BEGIN
  IF public.app_role() <> 'lead' THEN RAISE EXCEPTION 'lead only'; END IF;
  IF p_coder_a = p_coder_b THEN RAISE EXCEPTION 'choose two different coders'; END IF;
  IF coalesce(btrim(p_reason), '') = '' THEN RAISE EXCEPTION 'a reason is required'; END IF;
  IF (SELECT count(*) FROM public.split_submissions s WHERE s.review_id = p_review AND s.coder IN (p_coder_a, p_coder_b)) <> 2 THEN
    RAISE EXCEPTION 'both coders must have finished this review';
  END IF;
  IF EXISTS (SELECT 1 FROM public.human_labels l JOIN public.agreed_phrases a USING (phrase_id) WHERE a.review_id = p_review) THEN
    RAISE EXCEPTION 'coders have already labelled this review, so its phrases cannot be changed';
  END IF;
  SELECT x.text INTO t FROM public.reviews x WHERE x.review_id = p_review;
  IF t IS NULL THEN RAISE EXCEPTION 'unknown review'; END IF;
  FOR r IN SELECT q.s, q.e FROM (SELECT (x->>'start')::integer s, (x->>'end')::integer e FROM jsonb_array_elements(p_final) x) q ORDER BY q.s LOOP
    n := n + 1;
    IF r.s < prev OR r.e <= r.s OR r.e > char_length(t) THEN RAISE EXCEPTION 'phrases must be in order, inside the review and not overlap'; END IF;
    IF substr(t, r.s + 1, r.e - r.s) ~ '^\s|\s$' THEN RAISE EXCEPTION 'a phrase must not start or end with whitespace'; END IF;
    IF substr(t, prev + 1, r.s - prev) ~ '\S' THEN RAISE EXCEPTION 'every word must be in a phrase'; END IF;
    prev := r.e;
  END LOOP;
  IF n = 0 OR substr(t, prev + 1) ~ '\S' THEN RAISE EXCEPTION 'every word must be in a phrase'; END IF;
  SELECT coalesce(jsonb_agg(jsonb_build_object('start', h.start_offset, 'end', h.end_offset) ORDER BY h.start_offset), '[]') INTO sa
    FROM public.human_splits h WHERE h.review_id = p_review AND h.coder = p_coder_a;
  SELECT coalesce(jsonb_agg(jsonb_build_object('start', h.start_offset, 'end', h.end_offset) ORDER BY h.start_offset), '[]') INTO sb
    FROM public.human_splits h WHERE h.review_id = p_review AND h.coder = p_coder_b;
  INSERT INTO public.adjudication_log (review_id, coder_a, coder_b, split_a, split_b, final_split, reason, decided_by, codebook_gap, codebook_version)
    VALUES (p_review, p_coder_a, p_coder_b, sa, sb, p_final, btrim(p_reason), auth.uid(), p_gap, public.current_codebook_version());
  DELETE FROM public.agreed_phrases WHERE review_id = p_review;
  INSERT INTO public.agreed_phrases (review_id, start_offset, end_offset, phrase_text)
    SELECT p_review, q.s, q.e, substr(t, q.s + 1, q.e - q.s)
    FROM (SELECT (x->>'start')::integer s, (x->>'end')::integer e FROM jsonb_array_elements(p_final) x) q ORDER BY q.s;
END $$;

-- Reviews of a set where both coders finished with exactly the same phrases need no discussion: copy them to agreed_phrases.
CREATE OR REPLACE FUNCTION accept_identical(p_set text, p_coder_a uuid, p_coder_b uuid) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE n integer;
BEGIN
  IF public.app_role() <> 'lead' THEN RAISE EXCEPTION 'lead only'; END IF;
  IF p_coder_a = p_coder_b THEN RAISE EXCEPTION 'choose two different coders'; END IF;
  IF p_set NOT IN ('dev', 'test') THEN RAISE EXCEPTION 'only the development and test sets are adjudicated'; END IF;
  WITH same AS (
    SELECT r.review_id, r.text FROM public.reviews r
    WHERE ((p_set = 'dev' AND r.gold_set IN ('dev_set','supp_rel_dev')) OR (p_set = 'test' AND r.gold_set IN ('test_set','supp_rel_test')))
      AND NOT EXISTS (SELECT 1 FROM public.agreed_phrases a WHERE a.review_id = r.review_id)
      AND (SELECT count(*) FROM public.split_submissions s WHERE s.review_id = r.review_id AND s.coder IN (p_coder_a, p_coder_b)) = 2
      AND NOT EXISTS (SELECT 1 FROM ((SELECT start_offset, end_offset FROM public.human_splits WHERE review_id = r.review_id AND coder = p_coder_a)
                                     EXCEPT (SELECT start_offset, end_offset FROM public.human_splits WHERE review_id = r.review_id AND coder = p_coder_b)) d)
      AND NOT EXISTS (SELECT 1 FROM ((SELECT start_offset, end_offset FROM public.human_splits WHERE review_id = r.review_id AND coder = p_coder_b)
                                     EXCEPT (SELECT start_offset, end_offset FROM public.human_splits WHERE review_id = r.review_id AND coder = p_coder_a)) d)
      AND EXISTS (SELECT 1 FROM public.human_splits WHERE review_id = r.review_id AND coder = p_coder_a)
  ), ins AS (
    INSERT INTO public.agreed_phrases (review_id, start_offset, end_offset, phrase_text)
      SELECT h.review_id, h.start_offset, h.end_offset, substr(s.text, h.start_offset + 1, h.end_offset - h.start_offset)
      FROM public.human_splits h JOIN same s USING (review_id) WHERE h.coder = p_coder_a RETURNING review_id)
  SELECT count(DISTINCT review_id) INTO n FROM ins;
  RETURN n;
END $$;

-- ===== 9. Stage 2: coders label the agreed phrases (dimension + sentiment). Same rules as splitting: own rows only, locked after finishing =====
CREATE TABLE IF NOT EXISTS label_access (
  set_name text PRIMARY KEY CHECK (set_name IN ('dev','test')),
  is_open boolean NOT NULL DEFAULT false,
  changed_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO label_access (set_name) VALUES ('dev'), ('test') ON CONFLICT DO NOTHING;
ALTER TABLE label_access ENABLE ROW LEVEL SECURITY;

-- One label (dimension) and one sentiment per phrase and coder. Dimensions: TAN tangibles, REL reliability, RES responsiveness,
-- ASS assurance, EMP empathy, VAL value, LOC location, FNB food and beverage, GEN general service, UNC unclear, NON none.
CREATE TABLE IF NOT EXISTS human_labels (
  phrase_id bigint NOT NULL REFERENCES agreed_phrases(phrase_id) ON DELETE RESTRICT,
  coder uuid NOT NULL REFERENCES profiles(user_id) ON DELETE RESTRICT,
  dimension text NOT NULL CHECK (dimension IN ('TAN','REL','RES','ASS','EMP','VAL','LOC','FNB','GEN','UNC','NON')),
  sentiment text NOT NULL CHECK (sentiment IN ('positive','negative','neutral')),
  saved_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (phrase_id, coder)
);
ALTER TABLE human_labels ENABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS label_submissions (
  review_id bigint NOT NULL REFERENCES reviews(review_id) ON DELETE RESTRICT,
  coder uuid NOT NULL REFERENCES profiles(user_id) ON DELETE RESTRICT,
  submitted_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (review_id, coder)
);
ALTER TABLE label_submissions ENABLE ROW LEVEL SECURITY;

-- Lead only. Labelling of a set can open only when every review in it has agreed phrases (all splits adjudicated).
CREATE OR REPLACE FUNCTION set_label_open(p_set text, p_open boolean) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE missing integer;
BEGIN
  IF public.app_role() <> 'lead' THEN RAISE EXCEPTION 'lead only'; END IF;
  IF p_set NOT IN ('dev', 'test') THEN RAISE EXCEPTION 'only the development and test sets are labelled'; END IF;
  IF p_open THEN
    SELECT count(*) INTO missing FROM public.reviews r
     WHERE ((p_set = 'dev' AND r.gold_set IN ('dev_set','supp_rel_dev')) OR (p_set = 'test' AND r.gold_set IN ('test_set','supp_rel_test')))
       AND NOT EXISTS (SELECT 1 FROM public.agreed_phrases a WHERE a.review_id = r.review_id);
    IF missing > 0 THEN RAISE EXCEPTION '% review(s) have no agreed phrases yet; finish the split adjudication first', missing; END IF;
  END IF;
  UPDATE public.label_access SET is_open = p_open, changed_at = now() WHERE set_name = p_set;
END $$;

CREATE OR REPLACE FUNCTION unlock_labels(p_coder uuid, p_set text) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE n integer;
BEGIN
  IF public.app_role() <> 'lead' THEN RAISE EXCEPTION 'lead only'; END IF;
  DELETE FROM public.label_submissions s USING public.reviews r
   WHERE s.review_id = r.review_id AND s.coder = p_coder
     AND ((p_set = 'dev' AND r.gold_set IN ('dev_set','supp_rel_dev')) OR (p_set = 'test' AND r.gold_set IN ('test_set','supp_rel_test')));
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

-- The signed-in coder may label this phrase: labelling of its set is open and the coder has not finished the review.
CREATE OR REPLACE FUNCTION can_edit_label(p_phrase bigint) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS
$$ SELECT public.app_role() = 'coder'
     AND EXISTS (SELECT 1 FROM public.agreed_phrases a JOIN public.reviews r USING (review_id) JOIN public.label_access l
                   ON l.set_name = CASE WHEN r.gold_set IN ('dev_set','supp_rel_dev') AND NOT r.round_closed THEN 'dev' WHEN r.gold_set IN ('test_set','supp_rel_test') THEN 'test' END
                 WHERE a.phrase_id = p_phrase AND l.is_open
                   AND NOT EXISTS (SELECT 1 FROM public.label_submissions s WHERE s.review_id = a.review_id AND s.coder = auth.uid())) $$;

-- Coder queue for labelling: agreed phrases with the coder's own label. Review text only (no title, rating, hotel, other coder).
CREATE OR REPLACE FUNCTION label_queue(p_set text)
RETURNS TABLE (review_id bigint, lang text, body text, phrase_id bigint, start_offset integer, end_offset integer, phrase_text text,
               dimension text, sentiment text, finished boolean)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
BEGIN
  IF public.app_role() <> 'coder' THEN RAISE EXCEPTION 'coders only'; END IF;
  IF NOT EXISTS (SELECT 1 FROM public.label_access l WHERE l.set_name = p_set AND l.is_open) THEN RAISE EXCEPTION 'labelling of set % is not open', p_set; END IF;
  RETURN QUERY
    SELECT r.review_id, r.language, r.text, a.phrase_id, a.start_offset, a.end_offset, a.phrase_text, h.dimension, h.sentiment,
           EXISTS (SELECT 1 FROM public.label_submissions s WHERE s.review_id = r.review_id AND s.coder = auth.uid())
    FROM public.reviews r
    JOIN public.agreed_phrases a ON a.review_id = r.review_id
    LEFT JOIN public.human_labels h ON h.phrase_id = a.phrase_id AND h.coder = auth.uid()
    WHERE ((p_set = 'dev' AND r.gold_set IN ('dev_set','supp_rel_dev') AND NOT r.round_closed) OR (p_set = 'test' AND r.gold_set IN ('test_set','supp_rel_test')))
    ORDER BY md5(r.review_id::text), a.start_offset;
END $$;

CREATE OR REPLACE FUNCTION save_label(p_phrase bigint, p_dimension text, p_sentiment text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
BEGIN
  IF NOT public.can_edit_label(p_phrase) THEN RAISE EXCEPTION 'this phrase cannot be labelled now'; END IF;
  INSERT INTO public.human_labels (phrase_id, coder, dimension, sentiment) VALUES (p_phrase, auth.uid(), p_dimension, p_sentiment)
    ON CONFLICT (phrase_id, coder) DO UPDATE SET dimension = EXCLUDED.dimension, sentiment = EXCLUDED.sentiment, saved_at = now();
END $$;

-- Finish labelling a set: every phrase of every review must have a label; then the reviews are locked for this coder.
CREATE OR REPLACE FUNCTION finish_labels(p_set text) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE n integer; bad integer;
BEGIN
  IF public.app_role() <> 'coder' THEN RAISE EXCEPTION 'coders only'; END IF;
  IF NOT EXISTS (SELECT 1 FROM public.label_access l WHERE l.set_name = p_set AND l.is_open) THEN RAISE EXCEPTION 'labelling of set % is not open', p_set; END IF;
  SELECT count(*) INTO bad FROM public.reviews r JOIN public.agreed_phrases a USING (review_id)
   WHERE ((p_set = 'dev' AND r.gold_set IN ('dev_set','supp_rel_dev') AND NOT r.round_closed) OR (p_set = 'test' AND r.gold_set IN ('test_set','supp_rel_test')))
     AND NOT EXISTS (SELECT 1 FROM public.label_submissions s WHERE s.review_id = r.review_id AND s.coder = auth.uid())
     AND NOT EXISTS (SELECT 1 FROM public.human_labels h WHERE h.phrase_id = a.phrase_id AND h.coder = auth.uid());
  IF bad > 0 THEN RAISE EXCEPTION '% phrase(s) have no label yet', bad; END IF;
  INSERT INTO public.label_submissions (review_id, coder)
    SELECT DISTINCT r.review_id, auth.uid() FROM public.reviews r JOIN public.agreed_phrases a USING (review_id)
     WHERE (p_set = 'dev' AND r.gold_set IN ('dev_set','supp_rel_dev') AND NOT r.round_closed) OR (p_set = 'test' AND r.gold_set IN ('test_set','supp_rel_test'))
    ON CONFLICT DO NOTHING;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

-- ===== 10. Gold labels: labels both coders agree on, or the lead decides (with a reason and, if needed, a codebook-gap flag) =====
CREATE TABLE IF NOT EXISTS gold_labels (
  phrase_id bigint PRIMARY KEY REFERENCES agreed_phrases(phrase_id) ON DELETE RESTRICT,
  dimension text NOT NULL CHECK (dimension IN ('TAN','REL','RES','ASS','EMP','VAL','LOC','FNB','GEN','UNC','NON')),
  sentiment text NOT NULL CHECK (sentiment IN ('positive','negative','neutral')),
  by_coders boolean NOT NULL,        -- true: both coders chose the same, no discussion
  codebook_version text,
  decided_by uuid REFERENCES profiles(user_id) ON DELETE RESTRICT,
  decided_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE gold_labels ENABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS label_adjudication_log (
  log_id bigserial PRIMARY KEY,
  phrase_id bigint NOT NULL REFERENCES agreed_phrases(phrase_id) ON DELETE RESTRICT,
  coder_a uuid NOT NULL REFERENCES profiles(user_id) ON DELETE RESTRICT,
  coder_b uuid NOT NULL REFERENCES profiles(user_id) ON DELETE RESTRICT,
  label_a jsonb NOT NULL,            -- {"dimension":"TAN","sentiment":"positive"} copied from human_labels
  label_b jsonb NOT NULL,
  final_label jsonb NOT NULL,
  reason text NOT NULL CHECK (btrim(reason) <> ''),
  codebook_gap boolean NOT NULL DEFAULT false,
  codebook_version text,
  decided_by uuid NOT NULL REFERENCES profiles(user_id) ON DELETE RESTRICT,
  decided_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS label_adjudication_log_phrase_idx ON label_adjudication_log (phrase_id);
ALTER TABLE label_adjudication_log ENABLE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION save_label_adjudication(p_phrase bigint, p_coder_a uuid, p_coder_b uuid, p_dimension text, p_sentiment text, p_reason text, p_gap boolean DEFAULT false)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE la jsonb; lb jsonb; rid bigint;
BEGIN
  IF public.app_role() <> 'lead' THEN RAISE EXCEPTION 'lead only'; END IF;
  IF p_coder_a = p_coder_b THEN RAISE EXCEPTION 'choose two different coders'; END IF;
  IF coalesce(btrim(p_reason), '') = '' THEN RAISE EXCEPTION 'a reason is required'; END IF;
  SELECT a.review_id INTO rid FROM public.agreed_phrases a WHERE a.phrase_id = p_phrase;
  IF rid IS NULL THEN RAISE EXCEPTION 'unknown phrase'; END IF;
  IF (SELECT count(*) FROM public.label_submissions s WHERE s.review_id = rid AND s.coder IN (p_coder_a, p_coder_b)) <> 2 THEN
    RAISE EXCEPTION 'both coders must have finished labelling this review';
  END IF;
  SELECT jsonb_build_object('dimension', h.dimension, 'sentiment', h.sentiment) INTO la FROM public.human_labels h WHERE h.phrase_id = p_phrase AND h.coder = p_coder_a;
  SELECT jsonb_build_object('dimension', h.dimension, 'sentiment', h.sentiment) INTO lb FROM public.human_labels h WHERE h.phrase_id = p_phrase AND h.coder = p_coder_b;
  IF la IS NULL OR lb IS NULL THEN RAISE EXCEPTION 'both coders must have labelled this phrase'; END IF;
  INSERT INTO public.gold_labels (phrase_id, dimension, sentiment, by_coders, codebook_version, decided_by)
    VALUES (p_phrase, p_dimension, p_sentiment, false, public.current_codebook_version(), auth.uid())
    ON CONFLICT (phrase_id) DO UPDATE SET dimension = EXCLUDED.dimension, sentiment = EXCLUDED.sentiment, by_coders = false,
      codebook_version = EXCLUDED.codebook_version, decided_by = EXCLUDED.decided_by, decided_at = now();
  INSERT INTO public.label_adjudication_log (phrase_id, coder_a, coder_b, label_a, label_b, final_label, reason, codebook_gap, codebook_version, decided_by)
    VALUES (p_phrase, p_coder_a, p_coder_b, la, lb, jsonb_build_object('dimension', p_dimension, 'sentiment', p_sentiment), btrim(p_reason), p_gap,
            public.current_codebook_version(), auth.uid());
END $$;

-- Phrases where both coders chose the same dimension and sentiment need no discussion.
CREATE OR REPLACE FUNCTION accept_identical_labels(p_set text, p_coder_a uuid, p_coder_b uuid) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE n integer;
BEGIN
  IF public.app_role() <> 'lead' THEN RAISE EXCEPTION 'lead only'; END IF;
  IF p_coder_a = p_coder_b THEN RAISE EXCEPTION 'choose two different coders'; END IF;
  IF p_set NOT IN ('dev', 'test') THEN RAISE EXCEPTION 'only the development and test sets are adjudicated'; END IF;
  WITH ins AS (
    INSERT INTO public.gold_labels (phrase_id, dimension, sentiment, by_coders, codebook_version, decided_by)
    SELECT a.phrase_id, ha.dimension, ha.sentiment, true, public.current_codebook_version(), auth.uid()
    FROM public.agreed_phrases a
    JOIN public.reviews r ON r.review_id = a.review_id
    JOIN public.human_labels ha ON ha.phrase_id = a.phrase_id AND ha.coder = p_coder_a
    JOIN public.human_labels hb ON hb.phrase_id = a.phrase_id AND hb.coder = p_coder_b
    WHERE ((p_set = 'dev' AND r.gold_set IN ('dev_set','supp_rel_dev')) OR (p_set = 'test' AND r.gold_set IN ('test_set','supp_rel_test')))
      AND ha.dimension = hb.dimension AND ha.sentiment = hb.sentiment
      AND NOT EXISTS (SELECT 1 FROM public.gold_labels g WHERE g.phrase_id = a.phrase_id)
      AND (SELECT count(*) FROM public.label_submissions s WHERE s.review_id = a.review_id AND s.coder IN (p_coder_a, p_coder_b)) = 2
    RETURNING 1)
  SELECT count(*) INTO n FROM ins;
  RETURN n;
END $$;

-- ===== 6. Policies =====
DROP POLICY IF EXISTS profiles_self ON profiles;
CREATE POLICY profiles_self ON profiles FOR SELECT TO authenticated USING (user_id = auth.uid() OR public.app_role() IN ('lead','researcher'));

DROP POLICY IF EXISTS set_access_read ON set_access;
CREATE POLICY set_access_read ON set_access FOR SELECT TO authenticated USING (true);

DROP POLICY IF EXISTS splits_coder_select ON human_splits;
CREATE POLICY splits_coder_select ON human_splits FOR SELECT TO authenticated USING (coder = auth.uid());
DROP POLICY IF EXISTS splits_staff_select ON human_splits;
CREATE POLICY splits_staff_select ON human_splits FOR SELECT TO authenticated USING (public.app_role() IN ('lead','researcher'));
DROP POLICY IF EXISTS splits_coder_insert ON human_splits;
CREATE POLICY splits_coder_insert ON human_splits FOR INSERT TO authenticated WITH CHECK (coder = auth.uid() AND public.can_edit_split(review_id));
DROP POLICY IF EXISTS splits_coder_update ON human_splits;
CREATE POLICY splits_coder_update ON human_splits FOR UPDATE TO authenticated
  USING (coder = auth.uid() AND public.can_edit_split(review_id)) WITH CHECK (coder = auth.uid() AND public.can_edit_split(review_id));
DROP POLICY IF EXISTS splits_coder_delete ON human_splits;
CREATE POLICY splits_coder_delete ON human_splits FOR DELETE TO authenticated USING (coder = auth.uid() AND public.can_edit_split(review_id));

DROP POLICY IF EXISTS submissions_select ON split_submissions;
CREATE POLICY submissions_select ON split_submissions FOR SELECT TO authenticated USING (coder = auth.uid() OR public.app_role() IN ('lead','researcher'));

DROP POLICY IF EXISTS agreed_select ON agreed_phrases;
CREATE POLICY agreed_select ON agreed_phrases FOR SELECT TO authenticated USING (public.app_role() IN ('lead','researcher'));
DROP POLICY IF EXISTS agreed_lead_write ON agreed_phrases;
CREATE POLICY agreed_lead_write ON agreed_phrases FOR ALL TO authenticated USING (public.app_role() = 'lead') WITH CHECK (public.app_role() = 'lead');

DROP POLICY IF EXISTS label_access_read ON label_access;
CREATE POLICY label_access_read ON label_access FOR SELECT TO authenticated USING (true);
DROP POLICY IF EXISTS labels_coder_select ON human_labels;
CREATE POLICY labels_coder_select ON human_labels FOR SELECT TO authenticated USING (coder = auth.uid());
DROP POLICY IF EXISTS labels_staff_select ON human_labels;
CREATE POLICY labels_staff_select ON human_labels FOR SELECT TO authenticated USING (public.app_role() IN ('lead','researcher'));
DROP POLICY IF EXISTS label_subs_select ON label_submissions;
CREATE POLICY label_subs_select ON label_submissions FOR SELECT TO authenticated USING (coder = auth.uid() OR public.app_role() IN ('lead','researcher'));

DROP POLICY IF EXISTS gold_labels_select ON gold_labels;
CREATE POLICY gold_labels_select ON gold_labels FOR SELECT TO authenticated USING (public.app_role() IN ('lead','researcher'));
DROP POLICY IF EXISTS label_adj_log_select ON label_adjudication_log;
CREATE POLICY label_adj_log_select ON label_adjudication_log FOR SELECT TO authenticated USING (public.app_role() = 'lead');
ALTER TABLE codebook_versions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS codebook_versions_select ON codebook_versions;
CREATE POLICY codebook_versions_select ON codebook_versions FOR SELECT TO authenticated USING (public.app_role() IN ('lead','researcher'));

-- ===== 7. Grants (everything else stays revoked) =====
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC, anon, authenticated;   -- functions created above default to PUBLIC execute
GRANT SELECT ON profiles, set_access, split_submissions, agreed_phrases, adjudication_log TO authenticated;   -- agreed_phrases and the log are written only by the functions below
GRANT SELECT ON human_splits, label_access, human_labels, label_submissions, gold_labels, label_adjudication_log, codebook_versions TO authenticated;   -- writes go through save_splits only
GRANT EXECUTE ON FUNCTION app_role(), can_edit_split(bigint), coder_queue(text), save_splits(bigint, jsonb), finish_set(text), set_set_open(text, boolean),
                          unlock_set(uuid, text), review_meta(), save_adjudication(bigint, uuid, uuid, jsonb, text, boolean), accept_identical(text, uuid, uuid), set_label_open(text, boolean), unlock_labels(uuid, text),
                          save_label_adjudication(bigint, uuid, uuid, text, text, text, boolean), accept_identical_labels(text, uuid, uuid),
                          can_edit_label(bigint), label_queue(text), save_label(bigint, text, text), finish_labels(text) TO authenticated;

-- Refresh planner statistics: the sample columns were filled after the last automatic ANALYZE, and stale stats made the app queries scan the whole table.

-- ===== 11. Development rounds and codebook versions =====
-- Round 1 is the original draw. After a failed kappa and a newer codebook, the lead draws a fresh round: a reserve (never-used reviews,
-- seed 42 + round) is made beforehand by `plan-dev-rounds`, and draw_dev_round() switches to it. Old rounds keep their data but close for coders.
CREATE TABLE IF NOT EXISTS dev_rounds (
  round integer PRIMARY KEY,
  codebook_version text NOT NULL REFERENCES codebook_versions(version),
  drawn_at timestamptz NOT NULL DEFAULT now(),
  reason text NOT NULL,
  drawn_by uuid
);
ALTER TABLE dev_rounds ENABLE ROW LEVEL SECURITY;
INSERT INTO dev_rounds (round, codebook_version, reason) VALUES (1, '0.2', 'Original draw') ON CONFLICT DO NOTHING;
UPDATE reviews SET dev_round = 1 WHERE gold_set IN ('dev_set','supp_rel_dev') AND dev_round IS NULL;

CREATE TABLE IF NOT EXISTS dev_round_reserve (   -- no policy and no grant: only the Python backend and draw_dev_round() read it
  round integer NOT NULL,
  review_id bigint NOT NULL REFERENCES reviews(review_id),
  role text NOT NULL CHECK (role IN ('dev_set','supp_rel_dev')),
  PRIMARY KEY (round, review_id)
);
ALTER TABLE dev_round_reserve ENABLE ROW LEVEL SECURITY;

-- Lead only: add a codebook version (markdown replaces the whole codebook page). The version label must be new.
CREATE OR REPLACE FUNCTION upload_codebook(p_version text, p_body text, p_note text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
BEGIN
  IF public.app_role() <> 'lead' THEN RAISE EXCEPTION 'lead only'; END IF;
  IF btrim(coalesce(p_version, '')) = '' OR btrim(coalesce(p_body, '')) = '' THEN RAISE EXCEPTION 'a version label and the codebook text are required'; END IF;
  IF EXISTS (SELECT 1 FROM public.codebook_versions WHERE version = btrim(p_version)) THEN RAISE EXCEPTION 'version % already exists', btrim(p_version); END IF;
  INSERT INTO public.codebook_versions (version, created_on, change_log, body, uploaded_by) VALUES (btrim(p_version), current_date, btrim(coalesce(p_note, '')), p_body, auth.uid());
END $$;

-- Everyone signed in reads the codebook (coders need it). body is NULL for versions that live only in the app bundle.
CREATE OR REPLACE FUNCTION codebook_list() RETURNS TABLE (version text, created_at timestamptz, change_log text, body text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS
$$ SELECT c.version, c.created_at, c.change_log, c.body FROM public.codebook_versions c WHERE auth.uid() IS NOT NULL ORDER BY c.created_at DESC, c.version DESC $$;

-- Lead and researchers: the rounds, plus what the lead needs to decide whether a fresh round can be drawn.
CREATE OR REPLACE FUNCTION dev_round_status() RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
DECLARE cur integer; cv text; rv text; res integer; fin integer; tot integer;
BEGIN
  IF public.app_role() NOT IN ('lead','researcher') THEN RAISE EXCEPTION 'staff only'; END IF;
  SELECT max(round) INTO cur FROM public.dev_rounds;
  SELECT codebook_version INTO rv FROM public.dev_rounds WHERE round = cur;
  cv := public.current_codebook_version();
  SELECT count(*) INTO res FROM public.dev_round_reserve WHERE round = cur + 1;
  SELECT count(*) INTO tot FROM public.reviews x WHERE x.dev_round = cur AND x.gold_set IN ('dev_set','supp_rel_dev');
  SELECT count(*) INTO fin FROM (
    SELECT s.coder FROM public.label_submissions s JOIN public.reviews r USING (review_id)
     WHERE r.dev_round = cur AND r.gold_set IN ('dev_set','supp_rel_dev') GROUP BY s.coder HAVING count(*) = tot) f;
  RETURN jsonb_build_object('round', cur, 'roundVersion', rv, 'currentVersion', cv, 'newerCodebook', cv IS DISTINCT FROM rv, 'reserve', res, 'codersFinished', fin,
    'rounds', (SELECT coalesce(jsonb_agg(jsonb_build_object('round', d.round, 'version', d.codebook_version, 'at', d.drawn_at, 'reason', d.reason) ORDER BY d.round), '[]') FROM public.dev_rounds d));
END $$;

-- Lead only: switch to the next fresh round. Needs a newer codebook version, two coders who finished labelling the current round, and the reserve.
CREATE OR REPLACE FUNCTION draw_dev_round(p_reason text) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE st jsonb; nxt integer;
BEGIN
  IF public.app_role() <> 'lead' THEN RAISE EXCEPTION 'lead only'; END IF;
  IF btrim(coalesce(p_reason, '')) = '' THEN RAISE EXCEPTION 'a reason is required'; END IF;
  st := public.dev_round_status();
  IF NOT (st->>'newerCodebook')::boolean THEN RAISE EXCEPTION 'upload a newer codebook version first'; END IF;
  IF (st->>'codersFinished')::integer < 2 THEN RAISE EXCEPTION 'two coders must finish labelling the current round first'; END IF;
  nxt := (st->>'round')::integer + 1;
  IF (st->>'reserve')::integer = 0 THEN RAISE EXCEPTION 'no reserve drawn for round %; run plan-dev-rounds', nxt; END IF;
  IF EXISTS (SELECT 1 FROM public.dev_round_reserve q JOIN public.reviews r USING (review_id) WHERE q.round = nxt AND (r.is_training OR r.is_pilot OR r.gold_set IS NOT NULL))
    THEN RAISE EXCEPTION 'a reserve review has been used elsewhere; redo plan-dev-rounds'; END IF;
  UPDATE public.reviews SET round_closed = true WHERE gold_set IN ('dev_set','supp_rel_dev');
  UPDATE public.reviews r SET gold_set = q.role, dev_round = nxt FROM public.dev_round_reserve q WHERE q.round = nxt AND q.review_id = r.review_id;
  INSERT INTO public.dev_rounds (round, codebook_version, reason, drawn_by) VALUES (nxt, st->>'currentVersion', btrim(p_reason), auth.uid());
  UPDATE public.set_access SET is_open = true, changed_at = now() WHERE set_name = 'dev';
  UPDATE public.label_access SET is_open = false, changed_at = now() WHERE set_name = 'dev';
  RETURN nxt;
END $$;

-- Re-confirmation (D4): gold labels decided under an earlier codebook version are confirmed or changed by the lead under the current version.
CREATE TABLE IF NOT EXISTS gold_reconfirmations (
  id bigserial PRIMARY KEY,
  phrase_id bigint NOT NULL REFERENCES agreed_phrases(phrase_id) ON DELETE RESTRICT,
  old_dimension text NOT NULL, old_sentiment text NOT NULL, old_version text,
  new_dimension text NOT NULL, new_sentiment text NOT NULL, new_version text NOT NULL,
  reason text NOT NULL,
  decided_by uuid NOT NULL,
  decided_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE gold_reconfirmations ENABLE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION reconfirm_gold(p_phrase bigint, p_dimension text, p_sentiment text, p_reason text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE g public.gold_labels; cv text := public.current_codebook_version();
BEGIN
  IF public.app_role() <> 'lead' THEN RAISE EXCEPTION 'lead only'; END IF;
  IF btrim(coalesce(p_reason, '')) = '' THEN RAISE EXCEPTION 'a reason is required'; END IF;
  IF p_dimension NOT IN ('TAN','REL','RES','ASS','EMP','VAL','LOC','FNB','GEN','UNC','NON') OR p_sentiment NOT IN ('positive','negative','neutral') THEN RAISE EXCEPTION 'invalid label'; END IF;
  SELECT * INTO g FROM public.gold_labels WHERE phrase_id = p_phrase;
  IF NOT FOUND THEN RAISE EXCEPTION 'this phrase has no gold label yet'; END IF;
  INSERT INTO public.gold_reconfirmations (phrase_id, old_dimension, old_sentiment, old_version, new_dimension, new_sentiment, new_version, reason, decided_by)
    VALUES (p_phrase, g.dimension, g.sentiment, g.codebook_version, p_dimension, p_sentiment, cv, btrim(p_reason), auth.uid());
  UPDATE public.gold_labels SET dimension = p_dimension, sentiment = p_sentiment, codebook_version = cv, decided_by = auth.uid(), decided_at = now() WHERE phrase_id = p_phrase;
END $$;

-- Keep every outdated gold label as it is, under the current version (one reason for all).
CREATE OR REPLACE FUNCTION reconfirm_all_gold(p_reason text) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE n integer; cv text := public.current_codebook_version();
BEGIN
  IF public.app_role() <> 'lead' THEN RAISE EXCEPTION 'lead only'; END IF;
  IF btrim(coalesce(p_reason, '')) = '' THEN RAISE EXCEPTION 'a reason is required'; END IF;
  INSERT INTO public.gold_reconfirmations (phrase_id, old_dimension, old_sentiment, old_version, new_dimension, new_sentiment, new_version, reason, decided_by)
    SELECT phrase_id, dimension, sentiment, codebook_version, dimension, sentiment, cv, btrim(p_reason), auth.uid() FROM public.gold_labels WHERE codebook_version IS DISTINCT FROM cv;
  GET DIAGNOSTICS n = ROW_COUNT;
  UPDATE public.gold_labels SET codebook_version = cv, decided_by = auth.uid(), decided_at = now() WHERE codebook_version IS DISTINCT FROM cv;
  RETURN n;
END $$;

DROP POLICY IF EXISTS gold_reconfirmations_select ON gold_reconfirmations;
CREATE POLICY gold_reconfirmations_select ON gold_reconfirmations FOR SELECT TO authenticated USING (public.app_role() = 'lead');
DROP POLICY IF EXISTS dev_rounds_select ON dev_rounds;
CREATE POLICY dev_rounds_select ON dev_rounds FOR SELECT TO authenticated USING (public.app_role() IN ('lead','researcher'));
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC, anon;
GRANT SELECT ON gold_reconfirmations, dev_rounds TO authenticated;
GRANT EXECUTE ON FUNCTION upload_codebook(text,text,text), codebook_list(), dev_round_status(), draw_dev_round(text),
  reconfirm_gold(bigint,text,text,text), reconfirm_all_gold(text) TO authenticated;
ANALYZE reviews;
