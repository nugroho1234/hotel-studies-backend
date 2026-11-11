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
  num_reviews_gt_100 boolean NOT NULL DEFAULT false,
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
