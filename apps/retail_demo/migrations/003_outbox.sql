-- Only future INSERTs enqueue. Never backfill historical requests or receipts.
ALTER TABLE session_operations ADD COLUMN preparation_seq bigint GENERATED ALWAYS AS IDENTITY;
CREATE TABLE session_current_operations (
  session_id text PRIMARY KEY REFERENCES parlant_sessions(id) ON DELETE CASCADE,
  operation_id uuid NOT NULL REFERENCES return_operations(id),
  preparation_seq bigint NOT NULL
);
INSERT INTO session_current_operations
SELECT DISTINCT ON (b.session_id) b.session_id,b.operation_id,b.preparation_seq
FROM session_operations b JOIN parlant_sessions s ON s.id=b.session_id
ORDER BY b.session_id,b.last_event_offset DESC,b.operation_id;
CREATE FUNCTION select_prepared_operation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO session_current_operations SELECT NEW.session_id,NEW.operation_id,NEW.preparation_seq
    WHERE EXISTS(SELECT 1 FROM parlant_sessions WHERE id=NEW.session_id)
    ON CONFLICT(session_id) DO UPDATE SET operation_id=EXCLUDED.operation_id,preparation_seq=EXCLUDED.preparation_seq
    WHERE session_current_operations.preparation_seq<EXCLUDED.preparation_seq;
  RETURN NEW;
END $$;
CREATE TRIGGER select_prepared AFTER INSERT ON session_operations
  FOR EACH ROW EXECUTE FUNCTION select_prepared_operation();

-- Capability minted from server ToolContext, never a model customer/session field.
-- A replay of the same capability returns its original preparation.
CREATE TABLE tool_session_contexts (
  id uuid PRIMARY KEY, session_id text NOT NULL REFERENCES parlant_sessions(id) ON DELETE CASCADE,
  customer_id text NOT NULL REFERENCES customers(id),
  operation_id uuid REFERENCES return_operations(id),
  preparation jsonb,
  expires_at timestamptz NOT NULL DEFAULT now()+interval '5 minutes'
);
CREATE TABLE return_outbox (
  id uuid PRIMARY KEY, operation_id uuid NOT NULL REFERENCES return_operations(id),
  request_id uuid NOT NULL REFERENCES return_requests(id),
  session_id text NOT NULL, customer_id text NOT NULL REFERENCES customers(id),
  notification_type text NOT NULL CHECK(notification_type='return_submitted'),
  status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','delivered','failed')),
  attempts integer NOT NULL DEFAULT 0 CHECK(attempts>=0),
  max_attempts integer NOT NULL DEFAULT 5 CHECK(max_attempts BETWEEN 1 AND 10),
  next_attempt_at timestamptz NOT NULL DEFAULT now(), last_error text,
  receipt_event_id text, created_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz,
  manual_retries integer NOT NULL DEFAULT 0,
  UNIQUE(operation_id,notification_type),
  CHECK((status='delivered')=(receipt_event_id IS NOT NULL AND completed_at IS NOT NULL))
);
CREATE INDEX return_outbox_due ON return_outbox(next_attempt_at,id) WHERE status='pending';
CREATE INDEX return_outbox_session ON return_outbox(session_id,status);
CREATE UNIQUE INDEX parlant_event_outbox ON parlant_events((doc#>>'{metadata,outbox_id}'))
  WHERE doc#>>'{metadata,outbox_id}' IS NOT NULL;
CREATE FUNCTION enqueue_return_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE b session_operations%ROWTYPE;
BEGIN
  SELECT * INTO b FROM session_operations WHERE operation_id=NEW.operation_id;
  -- Legacy operator-only fixtures have no customer session and no notification.
  IF b.operation_id IS NULL THEN RETURN NEW; END IF;
  IF b.confirmation_event_id IS NULL OR b.customer_id<>NEW.customer_id OR NOT EXISTS(
    SELECT 1 FROM parlant_sessions WHERE id=b.session_id AND customer_id=NEW.customer_id) THEN
    RAISE EXCEPTION 'live owned session and actual confirmation required for notification' USING ERRCODE='23514';
  END IF;
  UPDATE session_operations SET request_id=NEW.id WHERE operation_id=NEW.operation_id;
  INSERT INTO return_outbox(id,operation_id,request_id,session_id,customer_id,notification_type)
    VALUES(gen_random_uuid(),NEW.operation_id,NEW.id,b.session_id,b.customer_id,'return_submitted');
  RETURN NEW;
END $$;
CREATE TRIGGER enqueue_return_receipt AFTER INSERT ON return_requests
  FOR EACH ROW EXECUTE FUNCTION enqueue_return_receipt();
