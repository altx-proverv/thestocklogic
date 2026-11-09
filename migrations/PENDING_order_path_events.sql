-- PENDING: not applied. Applying it was refused by the session permission
-- classifier on 2026-11-09; apply by hand (Supabase SQL editor or MCP), then
-- rename to a dated file. Until it exists, order_events writes only the local
-- reports/order_path_events.jsonl and logs each failed insert.

-- ORDER-PATH EVENTS — every refusal and every substitution on the order path.
--
-- WHY. On 2026-11-09 "was any real order placed on a substituted price?" could
-- not be answered: atlas_entry.py replaced a missing quote with entry_ref and
-- logged nothing. Written by atlas/execution/order_events.py. Read with:
--
--   select * from order_path_events where event = 'SUBSTITUTED';
--   select * from order_path_events where event = 'REFUSED' order by at desc;
--   select * from order_path_events where event = 'PRICE_USED' and trade_id = 123;
--
-- APPEND-ONLY, like the observation ledger: a record of what the order path did
-- is worthless if it can be edited afterwards. Service role only -- it carries
-- symbols and order ids, and the anon key ships in every page.

CREATE TABLE IF NOT EXISTS public.order_path_events (
  id            bigserial    PRIMARY KEY,
  at            timestamptz  NOT NULL,
  session_date  date         NOT NULL,
  event         text         NOT NULL CHECK (event IN ('REFUSED', 'SUBSTITUTED', 'PRICE_USED')),
  source        text         NOT NULL,
  symbol        text,
  code          text         NOT NULL,
  reason        text         NOT NULL,
  detail        jsonb        NOT NULL DEFAULT '{}'::jsonb,
  trade_id      bigint,
  received_at   timestamptz  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS order_path_events_session_idx
  ON public.order_path_events (session_date, event);
CREATE INDEX IF NOT EXISTS order_path_events_trade_idx
  ON public.order_path_events (trade_id) WHERE trade_id IS NOT NULL;

CREATE OR REPLACE FUNCTION public.order_path_events_append_only()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'order_path_events is append-only: % is refused', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

DROP TRIGGER IF EXISTS order_path_events_no_update_delete ON public.order_path_events;
CREATE TRIGGER order_path_events_no_update_delete
  BEFORE UPDATE OR DELETE ON public.order_path_events
  FOR EACH ROW EXECUTE FUNCTION public.order_path_events_append_only();
DROP TRIGGER IF EXISTS order_path_events_no_truncate ON public.order_path_events;
CREATE TRIGGER order_path_events_no_truncate
  BEFORE TRUNCATE ON public.order_path_events
  FOR EACH STATEMENT EXECUTE FUNCTION public.order_path_events_append_only();

ALTER TABLE public.order_path_events ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.order_path_events FROM PUBLIC, anon, authenticated;
REVOKE ALL ON SEQUENCE public.order_path_events_id_seq FROM PUBLIC, anon, authenticated;

COMMENT ON TABLE public.order_path_events IS
  'Every refusal (missing/zero/stale/unreadable input) and every substituted value on the ATLAS order path, plus the price and its source for every entry order. Append-only. Written by atlas/execution/order_events.py.';
