-- Durable Signal ingress liveness marker.

BEGIN;

CREATE TABLE IF NOT EXISTS signal_ingress_health (
  name            TEXT PRIMARY KEY,
  last_inbound_at TIMESTAMPTZ NOT NULL,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO signal_ingress_health (name, last_inbound_at, updated_at)
VALUES ('default', now(), now())
ON CONFLICT (name) DO NOTHING;

COMMIT;
