-- APP-S1: future changed drafts; no rewrite/backfill of old confirmations/requests.
ALTER TABLE return_operations ADD COLUMN superseded_by uuid REFERENCES return_operations(id);
ALTER TABLE return_operations ADD CONSTRAINT not_self_superseded CHECK(superseded_by IS NULL OR superseded_by<>id);
CREATE FUNCTION protect_return_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF ROW(OLD.id,OLD.customer_id,OLD.order_id,OLD.item_id,OLD.quantity,OLD.reason,OLD.quoted_unit_price_cents,OLD.created_at)
     IS DISTINCT FROM ROW(NEW.id,NEW.customer_id,NEW.order_id,NEW.item_id,NEW.quantity,NEW.reason,NEW.quoted_unit_price_cents,NEW.created_at) THEN
    RAISE EXCEPTION 'prepared parameter snapshot is immutable' USING ERRCODE='23514';
  END IF;
  IF OLD.superseded_by IS NOT NULL AND NEW.superseded_by IS DISTINCT FROM OLD.superseded_by THEN
    RAISE EXCEPTION 'supersession is immutable' USING ERRCODE='23514';
  END IF;
  IF NEW.superseded_by IS NOT NULL AND EXISTS(SELECT 1 FROM return_requests WHERE operation_id=OLD.id) THEN
    RAISE EXCEPTION 'submitted operation cannot be superseded' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER protect_snapshot BEFORE UPDATE ON return_operations FOR EACH ROW EXECUTE FUNCTION protect_return_snapshot();
-- Also guard direct/legacy request insertion, sharing the operation lock with
-- the existing reserve trigger and preserving its authorization/amount checks.
CREATE FUNCTION reject_superseded_submission() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE replaced uuid;
BEGIN
  SELECT superseded_by INTO replaced FROM return_operations WHERE id=NEW.operation_id FOR UPDATE;
  IF replaced IS NOT NULL THEN RAISE EXCEPTION 'operation superseded' USING ERRCODE='23514'; END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER reject_superseded BEFORE INSERT ON return_requests FOR EACH ROW EXECUTE FUNCTION reject_superseded_submission();
CREATE OR REPLACE FUNCTION confirm_session_operation(p_event_id text) RETURNS void LANGUAGE plpgsql AS $$
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
  IF op.superseded_by IS NOT NULL THEN RAISE EXCEPTION 'operation superseded' USING ERRCODE='23514'; END IF;
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
