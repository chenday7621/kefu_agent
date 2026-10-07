-- Native Parlant documents remain lossless JSONB; indexed fields are generated
-- from those documents, with sessions/events in separate physical tables.
CREATE TABLE parlant_sessions (
  id text PRIMARY KEY,
  doc jsonb NOT NULL CHECK (doc->>'id'=id),
  customer_id text GENERATED ALWAYS AS (doc->>'customer_id') STORED,
  agent_id text GENERATED ALWAYS AS (doc->>'agent_id') STORED,
  creation_utc text GENERATED ALWAYS AS (doc->>'creation_utc') STORED,
  next_offset bigint NOT NULL DEFAULT 0 CHECK (next_offset >= 0)
);
CREATE INDEX parlant_sessions_customer ON parlant_sessions(customer_id,creation_utc,id);
CREATE INDEX parlant_sessions_agent ON parlant_sessions(agent_id,creation_utc,id);
CREATE INDEX parlant_sessions_creation ON parlant_sessions(creation_utc,id);
CREATE TABLE parlant_events (
  id text PRIMARY KEY,
  doc jsonb NOT NULL CHECK (doc->>'id'=id),
  session_id text GENERATED ALWAYS AS (doc->>'session_id') STORED REFERENCES parlant_sessions(id),
  event_offset bigint GENERATED ALWAYS AS ((doc->>'offset')::bigint) STORED,
  event_kind text GENERATED ALWAYS AS (doc->>'kind') STORED,
  event_source text GENERATED ALWAYS AS (doc->>'source') STORED,
  trace_id text GENERATED ALWAYS AS (doc->>'trace_id') STORED,
  deleted boolean GENERATED ALWAYS AS ((doc->>'deleted')::boolean) STORED,
  CHECK (event_offset >= 0),
  UNIQUE(session_id,event_offset)
);
CREATE INDEX parlant_events_kind ON parlant_events(session_id,event_kind,event_offset);
CREATE INDEX parlant_events_visible ON parlant_events(session_id,deleted,event_offset);
CREATE INDEX parlant_events_trace ON parlant_events(session_id,trace_id,event_offset);
CREATE TABLE parlant_customers (id text PRIMARY KEY, doc jsonb NOT NULL CHECK(doc->>'id'=id));
CREATE TABLE parlant_customer_tags (id text PRIMARY KEY, doc jsonb NOT NULL CHECK(doc->>'id'=id));
CREATE TABLE parlant_variables (id text PRIMARY KEY, doc jsonb NOT NULL CHECK(doc->>'id'=id));
CREATE TABLE parlant_variable_tags (id text PRIMARY KEY, doc jsonb NOT NULL CHECK(doc->>'id'=id));
CREATE TABLE parlant_variable_values (id text PRIMARY KEY, doc jsonb NOT NULL CHECK(doc->>'id'=id));
CREATE UNIQUE INDEX parlant_value_key ON parlant_variable_values((doc->>'variable_id'),(doc->>'key'));
CREATE TABLE parlant_session_metadata (id text PRIMARY KEY, doc jsonb NOT NULL CHECK(doc->>'id'=id));
CREATE TABLE parlant_customer_metadata (id text PRIMARY KEY, doc jsonb NOT NULL CHECK(doc->>'id'=id));
CREATE TABLE parlant_variable_metadata (id text PRIMARY KEY, doc jsonb NOT NULL CHECK(doc->>'id'=id));

-- Audit associations survive an explicit native session deletion; the immutable
-- confirmation snapshot retains the actual customer event even if an admin
-- deletes that event. No model-supplied customer/session identity is accepted.
CREATE TABLE session_operations (
  session_id text NOT NULL,
  operation_id uuid NOT NULL REFERENCES return_operations(id),
  customer_id text NOT NULL REFERENCES customers(id),
  prepared_event_id text,
  last_event_offset bigint NOT NULL,
  confirmation_event_id text,
  confirmation_event jsonb,
  request_id uuid REFERENCES return_requests(id),
  PRIMARY KEY(session_id,operation_id),
  UNIQUE(operation_id),
  CHECK ((confirmation_event_id IS NULL) = (confirmation_event IS NULL))
);
CREATE INDEX session_operations_recent ON session_operations(session_id,customer_id,last_event_offset DESC);
CREATE TABLE parlant_imported_documents (
  table_name text NOT NULL,
  document_id text NOT NULL,
  source_sha256 text NOT NULL,
  PRIMARY KEY(table_name,document_id)
);
CREATE FUNCTION link_submitted_request() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  UPDATE session_operations SET request_id=NEW.id
    WHERE operation_id=NEW.operation_id AND customer_id=NEW.customer_id;
  RETURN NEW;
END $$;
CREATE TRIGGER link_submitted AFTER INSERT ON return_requests
  FOR EACH ROW EXECUTE FUNCTION link_submitted_request();

-- Called only after the actual native customer event has been INSERTed, in the
-- same transaction. If validation fails, that event and the counter roll back.
CREATE FUNCTION confirm_session_operation(p_event_id text) RETURNS void LANGUAGE plpgsql AS $$
DECLARE ev jsonb; sess jsonb; op return_operations%ROWTYPE; phrase text; pieces text[]; oid uuid; rid uuid;
BEGIN
  SELECT doc INTO ev FROM parlant_events WHERE id=p_event_id;
  SELECT doc INTO sess FROM parlant_sessions WHERE id=ev->>'session_id';
  IF ev->>'source'<>'customer' OR ev->>'kind'<>'message' THEN
    RAISE EXCEPTION 'actual customer event required' USING ERRCODE='23514';
  END IF;
  phrase := btrim(ev#>>'{data,message}'); pieces := regexp_split_to_array(phrase,'\s+');
  IF array_length(pieces,1)<>5 THEN
    RAISE EXCEPTION 'invalid confirmation phrase' USING ERRCODE='23514';
  END IF;
  BEGIN oid := pieces[2]::uuid;
  EXCEPTION WHEN invalid_text_representation THEN
    RAISE EXCEPTION 'invalid operation id' USING ERRCODE='23514';
  END;
  SELECT * INTO op FROM return_operations WHERE id=oid AND customer_id=sess->>'customer_id' FOR UPDATE;
  IF op.id IS NULL THEN RAISE EXCEPTION 'operation not owned' USING ERRCODE='23514'; END IF;
  IF phrase<>format('确认退货 %s 订单%s 明细%s 数量%s',op.id,op.order_id,op.item_id,op.quantity) THEN
    RAISE EXCEPTION 'confirmation mismatch' USING ERRCODE='23514';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM parlant_events e WHERE e.session_id=ev->>'session_id'
      AND e.event_kind='message' AND e.event_source='ai_agent' AND NOT e.deleted
      AND e.event_offset<(ev->>'offset')::bigint
      AND position(op.id::text IN e.doc#>>'{data,message}')>0
      AND position(op.order_id IN e.doc#>>'{data,message}')>0
      AND position(op.item_id IN e.doc#>>'{data,message}')>0) THEN
    RAISE EXCEPTION 'no prior assistant offer in this session' USING ERRCODE='23514';
  END IF;
  SELECT id INTO rid FROM return_requests WHERE operation_id=op.id;
  -- A replay of an already submitted operation can always be queried, but it
  -- must never authorize a different new application after expiry.
  IF rid IS NULL AND op.expires_at<now() THEN
    RAISE EXCEPTION 'operation expired' USING ERRCODE='23514';
  END IF;
  IF EXISTS(SELECT 1 FROM session_operations WHERE operation_id=op.id AND session_id<>ev->>'session_id') THEN
    RAISE EXCEPTION 'operation belongs to another session' USING ERRCODE='23514';
  END IF;
  INSERT INTO session_operations(session_id,operation_id,customer_id,last_event_offset,confirmation_event_id,confirmation_event,request_id)
    VALUES(ev->>'session_id',op.id,op.customer_id,(ev->>'offset')::bigint,p_event_id,ev,rid)
    ON CONFLICT(session_id,operation_id) DO UPDATE SET
      last_event_offset=EXCLUDED.last_event_offset,
      confirmation_event_id=COALESCE(session_operations.confirmation_event_id,EXCLUDED.confirmation_event_id),
      confirmation_event=COALESCE(session_operations.confirmation_event,EXCLUDED.confirmation_event),
      request_id=COALESCE(session_operations.request_id,EXCLUDED.request_id);
  IF op.confirmed_at IS NULL THEN
    UPDATE return_operations SET confirmed_at=now(),confirmation_source='parlant-human-message:'||(ev->>'session_id') WHERE id=op.id;
    INSERT INTO operation_logs(operation_id,customer_id,action,details) VALUES(op.id,op.customer_id,'confirmed',
      jsonb_build_object('source','parlant-human-message:'||(ev->>'session_id'),'confirmation_text',phrase,'confirmation_event_id',p_event_id));
  END IF;
END $$;
