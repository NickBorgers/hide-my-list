BEGIN;

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'hml_readonly') THEN
    CREATE ROLE hml_readonly WITH LOGIN PASSWORD 'hml_readonly';
  END IF;
END
$$;
GRANT CONNECT ON DATABASE hml TO hml_readonly;
GRANT USAGE ON SCHEMA public TO hml_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO hml_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO hml_readonly;

COMMIT;
