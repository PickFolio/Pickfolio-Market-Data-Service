CREATE TABLE IF NOT EXISTS bootstrap_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DROP TRIGGER IF EXISTS trg_bootstrap_state_updated_at ON bootstrap_state;
CREATE TRIGGER trg_bootstrap_state_updated_at
BEFORE UPDATE ON bootstrap_state
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();
