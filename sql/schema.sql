-- Mirrors the live Supabase tables (PRD §4). Safe to rerun.
CREATE TABLE IF NOT EXISTS hotels (
  hotel_id bigint PRIMARY KEY,
  date_scraped date,
  hotel_name text NOT NULL,
  destination text NOT NULL,
  star_class smallint NOT NULL CHECK (star_class BETWEEN 1 AND 5),
  num_reviews integer CHECK (num_reviews >= 0),
  review_url text NOT NULL UNIQUE,
  address text,
  num_reviews_ge_100 boolean NOT NULL DEFAULT false,
  google_query text,
  google_name text,
  google_detail_url text,
  google_star_rating smallint CHECK (google_star_rating BETWEEN 1 AND 5),
  google_star_class_label text,
  google_match_score real CHECK (google_match_score BETWEEN 0 AND 100),
  google_scraped_at timestamptz,
  star_diff smallint,
  star_check_status text CHECK (star_check_status IN
    ('agree','agree_half_star','disagree','google_missing','needs_manual_check','match_uncertain')),
  ta_displayed_class numeric(2,1) CHECK (ta_displayed_class BETWEEN 1 AND 5),
  final_star_rating smallint CHECK (final_star_rating BETWEEN 1 AND 5),
  in_sample_frame boolean NOT NULL DEFAULT false,
  cell_order smallint CHECK (cell_order >= 1),
  in_eligibility_test boolean NOT NULL DEFAULT false,
  scrape_status text NOT NULL DEFAULT 'not_scraped' CHECK (scrape_status IN
    ('not_scraped','needs_pass2','complete','not_needed')),
  n_raw_reviews integer CHECK (n_raw_reviews >= 0),
  n_out_of_window integer CHECK (n_out_of_window >= 0),
  n_other_language integer CHECK (n_other_language >= 0),
  n_eligible_reviews integer CHECK (n_eligible_reviews >= 0),
  is_eligible boolean,
  in_final_sample boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS hotels_cell_order_uq ON hotels (destination, final_star_rating, cell_order) WHERE cell_order IS NOT NULL;
CREATE INDEX IF NOT EXISTS hotels_cell_idx ON hotels (destination, final_star_rating);

CREATE TABLE IF NOT EXISTS scrape_runs (
  run_id text PRIMARY KEY,
  actor text NOT NULL,
  pass text CHECK (pass IN ('google','eligibility_test','1','2')),
  input jsonb,
  dataset_id text,
  started_at timestamptz,
  finished_at timestamptz,
  item_count integer CHECK (item_count >= 0),
  usage_usd numeric(10,4) CHECK (usage_usd >= 0)
);

CREATE TABLE IF NOT EXISTS reviews (
  review_id bigint PRIMARY KEY,
  hotel_id bigint NOT NULL REFERENCES hotels(hotel_id) ON DELETE RESTRICT,
  created_date date,
  published_date date NOT NULL,
  publish_platform text,
  rating smallint CHECK (rating BETWEEN 1 AND 5),
  language text,
  original_language text,
  translation_type text,
  title text,
  text text,
  url text,
  helpful_votes integer CHECK (helpful_votes >= 0),
  mcid bigint,
  trip_info_stay_date date,
  trip_info_trip_type text,
  subrating_value smallint CHECK (subrating_value BETWEEN 1 AND 5),
  subrating_rooms smallint CHECK (subrating_rooms BETWEEN 1 AND 5),
  subrating_location smallint CHECK (subrating_location BETWEEN 1 AND 5),
  subrating_cleanliness smallint CHECK (subrating_cleanliness BETWEEN 1 AND 5),
  subrating_service smallint CHECK (subrating_service BETWEEN 1 AND 5),
  subrating_sleep_quality smallint CHECK (subrating_sleep_quality BETWEEN 1 AND 5),
  subratings_other jsonb,
  photo_count smallint NOT NULL DEFAULT 0 CHECK (photo_count >= 0),
  user_id_hash text,
  user_hometown text,
  user_contribution_count integer CHECK (user_contribution_count >= 0),
  owner_response_id bigint,
  owner_response_published_date date,
  owner_response_language text,
  owner_response_text text,
  owner_response_connection_to_subject text,
  input_page_number integer CHECK (input_page_number >= 1),
  scrape_run_id text REFERENCES scrape_runs(run_id),
  scraped_at timestamptz NOT NULL DEFAULT now(),
  lang_ok boolean,
  is_selected boolean NOT NULL DEFAULT false,
  selection_rank smallint CHECK (selection_rank >= 1),
  is_pilot boolean NOT NULL DEFAULT false,
  pilot_stratum text
);
CREATE INDEX IF NOT EXISTS reviews_hotel_date_idx ON reviews (hotel_id, published_date DESC, review_id DESC);
CREATE INDEX IF NOT EXISTS reviews_selected_idx ON reviews (hotel_id) WHERE is_selected;
CREATE UNIQUE INDEX IF NOT EXISTS reviews_selection_rank_uq ON reviews (hotel_id, selection_rank) WHERE selection_rank IS NOT NULL;

-- ===== Papers 2-3 extension (additive; nothing dropped; safe to rerun) =====
ALTER TABLE hotels ADD COLUMN IF NOT EXISTS google_entity_id text;
ALTER TABLE hotels ADD COLUMN IF NOT EXISTS google_kg_id text;
ALTER TABLE hotels ADD COLUMN IF NOT EXISTS exclusion_reason text;
ALTER TABLE hotels DROP CONSTRAINT IF EXISTS hotels_exclusion_reason_check;
ALTER TABLE hotels ADD CONSTRAINT hotels_exclusion_reason_check
  CHECK (exclusion_reason IN ('star_disagree','closed','not_a_hotel'));
ALTER TABLE hotels ADD COLUMN IF NOT EXISTS brand_or_chain text;
ALTER TABLE hotels ADD COLUMN IF NOT EXISTS room_count integer;
ALTER TABLE hotels ADD COLUMN IF NOT EXISTS amenities jsonb;
ALTER TABLE hotels ADD COLUMN IF NOT EXISTS latitude numeric(9,6);
ALTER TABLE hotels ADD COLUMN IF NOT EXISTS longitude numeric(9,6);

ALTER TABLE reviews ADD COLUMN IF NOT EXISTS stay_month_only boolean NOT NULL DEFAULT true;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS source_actor text;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS in_paper1_sample boolean GENERATED ALWAYS AS (is_selected) STORED;

ALTER TABLE scrape_runs DROP CONSTRAINT IF EXISTS scrape_runs_pass_check;
ALTER TABLE scrape_runs ADD CONSTRAINT scrape_runs_pass_check
  CHECK (pass IN ('google','eligibility_test','1','2','incremental','prices'));
ALTER TABLE scrape_runs ADD COLUMN IF NOT EXISTS status text;
ALTER TABLE scrape_runs ADD COLUMN IF NOT EXISTS error text;

-- the view expands h.* when created, so it is dropped and rebuilt to pick up new hotels columns
ALTER TABLE hotels ADD COLUMN IF NOT EXISTS has_google_price boolean;  -- false = Google Hotels shows no price (hotel stays in Paper 1); NULL = not checked
DROP VIEW IF EXISTS hotels_v;
CREATE VIEW hotels_v AS
SELECT h.*,
  h.star_class AS tripadvisor_class,
  h.google_star_rating AS google_class,
  h.cell_order AS draw_order,
  CASE WHEN h.exclusion_reason IS NOT NULL THEN 'excluded'
       WHEN h.scrape_status = 'not_needed' THEN 'not_needed'
       WHEN h.is_eligible IS TRUE THEN 'eligible'
       WHEN h.is_eligible IS FALSE THEN 'ineligible'
       ELSE 'pending' END AS eligibility_status
FROM hotels h;

CREATE TABLE IF NOT EXISTS prices (
  price_id bigserial PRIMARY KEY,
  hotel_id bigint NOT NULL REFERENCES hotels(hotel_id) ON DELETE RESTRICT,
  collection_run_id text NOT NULL REFERENCES scrape_runs(run_id),
  collected_at timestamptz NOT NULL DEFAULT now(),
  checkin_date date NOT NULL,
  checkout_date date NOT NULL,
  nights smallint,
  lead_days integer,
  day_type text CHECK (day_type IN ('weekday','weekend')),
  price_usd numeric(12,2),
  has_decimals boolean,
  status text CHECK (status IN ('ok','sold_out','missing')),
  raw_json jsonb,
  UNIQUE (collection_run_id, hotel_id, checkin_date, checkout_date)
);
CREATE INDEX IF NOT EXISTS prices_hotel_checkin_idx ON prices (hotel_id, checkin_date);

CREATE TABLE IF NOT EXISTS review_windows (
  hotel_id bigint NOT NULL REFERENCES hotels(hotel_id) ON DELETE RESTRICT,
  window_end_month date NOT NULL,
  review_count integer CHECK (review_count >= 0),
  pos_rate_tangibles real,
  neg_rate_tangibles real,
  pos_rate_reliability real,
  neg_rate_reliability real,
  pos_rate_responsiveness real,
  neg_rate_responsiveness real,
  pos_rate_assurance real,
  neg_rate_assurance real,
  pos_rate_empathy real,
  neg_rate_empathy real,
  PRIMARY KEY (hotel_id, window_end_month)
);

CREATE TABLE IF NOT EXISTS codebook_versions (
  version text PRIMARY KEY,
  created_on date,
  sha256 text,
  change_log text
);

CREATE TABLE IF NOT EXISTS prompt_versions (
  prompt_version text PRIMARY KEY,
  codebook_version text REFERENCES codebook_versions(version),
  prompt_sha256 text,
  frozen_at timestamptz
);

CREATE TABLE IF NOT EXISTS llm_phrase_codes (
  phrase_id bigserial PRIMARY KEY,
  review_id bigint NOT NULL REFERENCES reviews(review_id) ON DELETE RESTRICT,
  run_id text,
  span_start integer,
  span_end integer,
  rewritten_unit text,
  target text,
  sentiment text,
  model_name text NOT NULL,
  model_version text NOT NULL,
  params jsonb NOT NULL,
  prompt_version text REFERENCES prompt_versions(prompt_version),
  coded_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS llm_phrase_codes_review_idx ON llm_phrase_codes (review_id);

CREATE TABLE IF NOT EXISTS validation_split (
  review_id bigint PRIMARY KEY REFERENCES reviews(review_id) ON DELETE RESTRICT,
  split text CHECK (split IN ('training','dev','test')),
  stratum text,
  seed integer,
  assigned_at timestamptz NOT NULL DEFAULT now()
);

-- Pilot sample for hand-coding practice: pilot_id is the randomised label shown to the coder.
CREATE TABLE IF NOT EXISTS pilot_sample (
  pilot_id integer PRIMARY KEY CHECK (pilot_id >= 1),
  review_id bigint NOT NULL UNIQUE REFERENCES reviews(review_id) ON DELETE RESTRICT,
  stratum text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS hotels_google_entity_uq ON hotels (google_entity_id) WHERE google_entity_id IS NOT NULL;

-- Sample roles for Paper 1 (pilot is is_pilot above): training, and gold standard split into dev_set/test_set.
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS is_training boolean NOT NULL DEFAULT false;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS gold_set text;
ALTER TABLE reviews DROP CONSTRAINT IF EXISTS reviews_gold_set_chk;
ALTER TABLE reviews ADD CONSTRAINT reviews_gold_set_chk CHECK (gold_set IN ('dev_set','test_set'));
ALTER TABLE reviews DROP CONSTRAINT IF EXISTS reviews_role_excl_chk;
ALTER TABLE reviews ADD CONSTRAINT reviews_role_excl_chk CHECK (
  NOT (is_pilot AND is_training) AND (gold_set IS NULL OR NOT (is_pilot OR is_training)));
