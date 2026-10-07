CREATE TABLE customers (
  id text PRIMARY KEY,
  name text NOT NULL
);
CREATE TABLE orders (
  id text PRIMARY KEY,
  customer_id text NOT NULL REFERENCES customers(id),
  status text NOT NULL CHECK (status IN ('pending','delivered','cancelled')),
  created_at timestamptz NOT NULL DEFAULT now(),
  delivered_at timestamptz,
  currency text NOT NULL DEFAULT 'CNY' CHECK (currency = 'CNY'),
  UNIQUE (id, customer_id),
  CHECK (status <> 'delivered' OR delivered_at IS NOT NULL)
);
CREATE TABLE order_items (
  id text PRIMARY KEY,
  order_id text NOT NULL REFERENCES orders(id),
  product_name text NOT NULL,
  variant text NOT NULL,
  quantity integer NOT NULL CHECK (quantity BETWEEN 1 AND 1000),
  unit_price_cents integer NOT NULL CHECK (unit_price_cents BETWEEN 0 AND 100000000),
  returnable boolean NOT NULL DEFAULT true,
  reserved_return_quantity integer NOT NULL DEFAULT 0,
  UNIQUE (order_id, id),
  CHECK (reserved_return_quantity BETWEEN 0 AND quantity)
);
CREATE TABLE return_operations (
  id uuid PRIMARY KEY,
  customer_id text NOT NULL,
  order_id text NOT NULL,
  item_id text NOT NULL,
  quantity integer NOT NULL CHECK (quantity BETWEEN 1 AND 1000),
  reason text NOT NULL CHECK (length(reason) BETWEEN 1 AND 500),
  quoted_unit_price_cents integer NOT NULL CHECK (quoted_unit_price_cents >= 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL DEFAULT (now() + interval '30 minutes'),
  confirmed_at timestamptz,
  confirmation_source text,
  FOREIGN KEY (order_id, customer_id) REFERENCES orders(id, customer_id),
  FOREIGN KEY (order_id, item_id) REFERENCES order_items(order_id, id),
  CHECK ((confirmed_at IS NULL) = (confirmation_source IS NULL))
);
CREATE TABLE return_requests (
  id uuid PRIMARY KEY,
  operation_id uuid NOT NULL UNIQUE REFERENCES return_operations(id),
  customer_id text NOT NULL,
  order_id text NOT NULL,
  item_id text NOT NULL,
  quantity integer NOT NULL CHECK (quantity > 0),
  reason text NOT NULL,
  amount_cents bigint NOT NULL CHECK (amount_cents >= 0),
  status text NOT NULL DEFAULT 'submitted' CHECK (status = 'submitted'),
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (order_id, customer_id) REFERENCES orders(id, customer_id),
  FOREIGN KEY (order_id, item_id) REFERENCES order_items(order_id, id),
  UNIQUE (item_id) -- first version: one pending application per order line
);
CREATE TABLE operation_logs (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  operation_id uuid NOT NULL REFERENCES return_operations(id),
  customer_id text NOT NULL REFERENCES customers(id),
  action text NOT NULL CHECK (action IN ('prepared','confirmed','submitted')),
  details jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (operation_id, action)
);

-- Database enforcement complements business checks; a lock serializes competing writes.
CREATE FUNCTION reserve_return_quantity() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE item order_items%ROWTYPE; ord orders%ROWTYPE; op return_operations%ROWTYPE;
BEGIN
  SELECT * INTO op FROM return_operations WHERE id=NEW.operation_id FOR UPDATE;
  SELECT * INTO ord FROM orders WHERE id=NEW.order_id;
  SELECT * INTO item FROM order_items WHERE id=NEW.item_id AND order_id=NEW.order_id FOR UPDATE;
  IF op.id IS NULL OR item.id IS NULL OR ord.id IS NULL OR ord.customer_id<>NEW.customer_id
     OR op.customer_id<>NEW.customer_id OR op.order_id<>NEW.order_id OR op.item_id<>NEW.item_id
     OR op.quantity<>NEW.quantity OR op.reason<>NEW.reason OR op.confirmed_at IS NULL
     OR op.expires_at<now() THEN
    RAISE EXCEPTION 'invalid or unconfirmed return operation' USING ERRCODE='23514';
  END IF;
  IF ord.status<>'delivered' OR ord.delivered_at<now()-interval '30 days'
     OR ord.delivered_at>now() OR NOT item.returnable THEN
    RAISE EXCEPTION 'return ineligible' USING ERRCODE='23514';
  END IF;
  IF NEW.quantity+item.reserved_return_quantity>item.quantity THEN
    RAISE EXCEPTION 'return quantity exceeded' USING ERRCODE='23514';
  END IF;
  IF NEW.amount_cents<>item.unit_price_cents::bigint*NEW.quantity
     OR op.quoted_unit_price_cents<>item.unit_price_cents THEN
    RAISE EXCEPTION 'incorrect return amount' USING ERRCODE='23514';
  END IF;
  UPDATE order_items SET reserved_return_quantity=reserved_return_quantity+NEW.quantity WHERE id=item.id;
  RETURN NEW;
END $$;
CREATE TRIGGER reserve_return BEFORE INSERT ON return_requests
  FOR EACH ROW EXECUTE FUNCTION reserve_return_quantity();
CREATE FUNCTION immutable_return_request() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'submitted applications are immutable in this demo' USING ERRCODE='23514';
END $$;
CREATE TRIGGER immutable_return BEFORE UPDATE OR DELETE ON return_requests
  FOR EACH ROW EXECUTE FUNCTION immutable_return_request();
