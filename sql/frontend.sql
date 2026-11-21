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
                  WHERE r.review_id = p_review AND a.is_open) $$;

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
    FROM public.reviews r WHERE r.is_selected AND ((p_set = 'training' AND r.is_training) OR (p_set = 'dev' AND r.gold_set IN ('dev_set','supp_rel_dev'))
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
  FROM public.reviews r WHERE r.is_selected AND ((p_set = 'training' AND r.is_training) OR (p_set = 'dev' AND r.gold_set IN ('dev_set','supp_rel_dev'))
                                OR (p_set = 'test' AND r.gold_set IN ('test_set','supp_rel_test')))
    AND NOT EXISTS (SELECT 1 FROM public.split_submissions s WHERE s.review_id = r.review_id AND s.coder = auth.uid());
  IF bad > 0 THEN RAISE EXCEPTION '% review(s) are not fully split yet', bad; END IF;
  INSERT INTO public.split_submissions (review_id, coder)
    SELECT r.review_id, auth.uid() FROM public.reviews r WHERE r.is_selected AND ((p_set = 'training' AND r.is_training) OR (p_set = 'dev' AND r.gold_set IN ('dev_set','supp_rel_dev'))
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
CREATE OR REPLACE FUNCTION review_meta() RETURNS TABLE (review_id bigint, lang text, set_name text, body text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
BEGIN
  IF public.app_role() NOT IN ('lead','researcher') THEN RAISE EXCEPTION 'staff only'; END IF;
  RETURN QUERY SELECT r.review_id, r.language, public.review_set(r.is_training, r.gold_set), r.text
               FROM public.reviews r WHERE r.is_training OR r.gold_set IS NOT NULL;   -- no is_selected test: lets Postgres use the two partial indexes
END $$;

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
CREATE INDEX IF NOT EXISTS adjudication_log_review_idx ON adjudication_log (review_id);
ALTER TABLE adjudication_log ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS adjudication_log_select ON adjudication_log;
CREATE POLICY adjudication_log_select ON adjudication_log FOR SELECT TO authenticated USING (public.app_role() IN ('lead','researcher'));

-- Save the agreed split of one review. Both coders must have finished it. The two original splits are copied from human_splits
-- here (not taken from the browser), so the log cannot be edited from the page. Re-deciding a review adds a new log row.
CREATE OR REPLACE FUNCTION save_adjudication(p_review bigint, p_coder_a uuid, p_coder_b uuid, p_final jsonb, p_reason text)
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
  INSERT INTO public.adjudication_log (review_id, coder_a, coder_b, split_a, split_b, final_split, reason, decided_by)
    VALUES (p_review, p_coder_a, p_coder_b, sa, sb, p_final, btrim(p_reason), auth.uid());
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
                   ON l.set_name = CASE WHEN r.gold_set IN ('dev_set','supp_rel_dev') THEN 'dev' WHEN r.gold_set IN ('test_set','supp_rel_test') THEN 'test' END
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
    WHERE ((p_set = 'dev' AND r.gold_set IN ('dev_set','supp_rel_dev')) OR (p_set = 'test' AND r.gold_set IN ('test_set','supp_rel_test')))
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
   WHERE ((p_set = 'dev' AND r.gold_set IN ('dev_set','supp_rel_dev')) OR (p_set = 'test' AND r.gold_set IN ('test_set','supp_rel_test')))
     AND NOT EXISTS (SELECT 1 FROM public.label_submissions s WHERE s.review_id = r.review_id AND s.coder = auth.uid())
     AND NOT EXISTS (SELECT 1 FROM public.human_labels h WHERE h.phrase_id = a.phrase_id AND h.coder = auth.uid());
  IF bad > 0 THEN RAISE EXCEPTION '% phrase(s) have no label yet', bad; END IF;
  INSERT INTO public.label_submissions (review_id, coder)
    SELECT DISTINCT r.review_id, auth.uid() FROM public.reviews r JOIN public.agreed_phrases a USING (review_id)
     WHERE (p_set = 'dev' AND r.gold_set IN ('dev_set','supp_rel_dev')) OR (p_set = 'test' AND r.gold_set IN ('test_set','supp_rel_test'))
    ON CONFLICT DO NOTHING;
  GET DIAGNOSTICS n = ROW_COUNT;
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

-- ===== 7. Grants (everything else stays revoked) =====
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC, anon, authenticated;   -- functions created above default to PUBLIC execute
GRANT SELECT ON profiles, set_access, split_submissions, agreed_phrases, adjudication_log TO authenticated;   -- agreed_phrases and the log are written only by the functions below
GRANT SELECT ON human_splits, label_access, human_labels, label_submissions TO authenticated;   -- writes go through save_splits only
GRANT EXECUTE ON FUNCTION app_role(), can_edit_split(bigint), coder_queue(text), save_splits(bigint, jsonb), finish_set(text), set_set_open(text, boolean),
                          unlock_set(uuid, text), review_meta(), save_adjudication(bigint, uuid, uuid, jsonb, text), accept_identical(text, uuid, uuid), set_label_open(text, boolean), unlock_labels(uuid, text),
                          can_edit_label(bigint), label_queue(text), save_label(bigint, text, text), finish_labels(text) TO authenticated;

-- Refresh planner statistics: the sample columns were filled after the last automatic ANALYZE, and stale stats made the app queries scan the whole table.
ANALYZE reviews;
