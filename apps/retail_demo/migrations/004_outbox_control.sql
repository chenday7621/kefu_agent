CREATE TABLE outbox_control (singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), enabled boolean NOT NULL DEFAULT true);
INSERT INTO outbox_control VALUES(true,true);
ALTER TABLE return_outbox ADD COLUMN total_attempts integer NOT NULL DEFAULT 0;
CREATE TABLE return_outbox_errors (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  outbox_id uuid NOT NULL REFERENCES return_outbox(id),
  failed_at timestamptz NOT NULL DEFAULT now(), error text NOT NULL
);
