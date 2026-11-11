# Software Build Plan (M0-6/7, M1)

Source of truth for behaviour: [PRD](PRD.md) and [Implementation plan](IMPLEMENTATION_PLAN.md).
This document fixes the **module map, public interfaces and build order** so that components built in
parallel fit together. Everything runs on the fakes (`hardware/fake.py`); no Pi, network or Telegram token.

## Conventions

- Python 3.11, `src/feeder` layout, `asyncio` everywhere except SQLite (sync, local, fast).
- All stored/compared datetimes are **aware UTC**; local time only at the edges (day boundary, schedule slots, display).
- Time is injected: `NowFn = Callable[[], datetime]` (see `ports.py`). Tests use `tests/conftest.py::FakeNow`.
- Shared value types live in `models.py` (`Source`, `EventType`, `Event`, `Schedule`, `DispenseStatus`,
  `DispenseResult`, `SupplyStatus`); shared Protocols in `ports.py` (`NowFn`, `Alerts`, `TreatCounter`).
- Owner-facing text is plain strings built by the component that owns the behaviour.
- Tests: `pytest` + `pytest-asyncio` (auto mode). No network, no real Telegram, no sleeping (inject `sleep`).

## Module map

```
src/feeder/
  models.py            [scaffold]  shared enums/dataclasses
  ports.py             [scaffold]  NowFn, Alerts, TreatCounter protocols
  config.py            M1-2
  hardware/base.py     M1-1        HAL ABCs
  hardware/fake.py     M1-1        in-memory scriptable implementations
  storage/db.py        M1-3        SQLite + migrations + queries
  core/policy.py       M1-4        DailyCapPolicy
  core/feeder.py       M1-5        Feeder.dispense
  core/supply.py       M1-6        SupplyMonitor
  core/clock.py        M1-7        ClockGuard
  adapters/scheduler.py M1-8       FeedScheduler (APScheduler)
  adapters/leds.py     M1-9        LedController
  adapters/telegram_bot.py M1-10   TelegramBot
  adapters/buttons.py  M1-12       treat button -> dispense
  notify.py            M1-11       Notifier, build_daily_summary
  app.py / __main__.py M1-12       wiring, logging, CLI
deploy/ , Makefile     M1-13
```

## Public interfaces

### hardware/base.py (M1-1)

```python
class Led(StrEnum):      POWER="power"; CONNECTED="connected"; SUPPLY="supply"
class LedMode(StrEnum):  OFF="off"; SOLID="solid"; BLINK="blink"

class Motor(ABC):
    async def advance_pocket(self) -> None      # rotate exactly one pocket pitch, then de-energise
    def release(self) -> None                   # de-energise coils

class DropSensor(ABC):
    def arm(self) -> None                       # reset drop counter before a move
    async def wait_for_drop(self, timeout_s: float) -> int   # drops counted since arm(); 0 on timeout.
                                                              # Returns as soon as >=1 drop is seen.
class LevelSensor(ABC):
    async def read_distance_mm(self) -> int | None   # one sample; None = read failed

class StatusLeds(ABC):
    def set(self, led: Led, mode: LedMode, brightness: float = 1.0) -> None   # brightness 0..1

class Buttons(ABC):
    def on_treat_press(self, callback: Callable[[], None]) -> None  # callback may fire from any thread
```

`hardware/fake.py`:

```python
class FakeMotor(Motor):        advances: int; released: int; fail_next: bool(optional)
class FakeDropSensor(DropSensor):   # linked to a FakeMotor
    # default: every advance_pocket produces 1 drop. Scriptable per advance:
    def script(self, *drops_per_advance: int) -> None     # e.g. script(0, 0) = two empty pockets
    def set_default_drops(self, n: int) -> None
class FakeLevelSensor(LevelSensor):
    def set_distance(self, mm: int | None) -> None
    def script(self, *values: int | None) -> None          # consumed one per read, then last value repeats
    reads: int
class FakeStatusLeds(StatusLeds):   state: dict[Led, tuple[LedMode, float]]; history: list[...]
class FakeButtons(Buttons):         def press_treat(self) -> None   # invokes registered callback
class FakeHardware:                 motor, drop_sensor, level_sensor, leds, buttons  (pre-linked)
    @classmethod def create(cls) -> "FakeHardware"
```

### config.py (M1-2)

```python
class Config(BaseModel):       # pydantic v2, extra="forbid"
    telegram: TelegramConfig      # allowed_user_ids: list[int] (non-empty)
    timezone: str                 # validated IANA name; property .tz -> ZoneInfo
    treats: TreatsConfig          # daily_cap=8 (>=1), capacity=80 (>=1)
    supply: SupplyConfig          # low_threshold_pct=20, low_clear_pct=30 (> low), refill_rise_pct=15,
                                  # poll_interval_s=300, empty_mm|None, full_mm|None (both or neither; empty>full)
    schedule: ScheduleConfig      # default_jitter_min=0
    notifications: NotificationsConfig   # daily_summary: datetime.time (parsed from "HH:MM")
    leds: LedsConfig              # brightness 0..1
    pins: dict[str, int] = {}     # optional pin-map overrides (unused by fakes)
    db_path: str = "/var/lib/pet-treat-feeder/feeder.db"
    telegram_token: str           # from env TELEGRAM_BOT_TOKEN (not in YAML)

def load_config(config_path: str | Path = "config.yaml", env_path: str | Path | None = ".env",
                environ: Mapping[str, str] | None = None) -> Config
    # .env loaded WITHOUT mutating os.environ; process env (`environ`, default os.environ) wins over .env.
    # raises ConfigError (ValueError subclass) with a readable message on missing file/token/invalid values.
```

### storage/db.py (M1-3)

```python
class Database:                      # implements TreatCounter
    @classmethod def open(cls, path: str | Path = ":memory:") -> "Database"   # WAL (file DBs), foreign keys, migrations
    def close(self) -> None
    schema_version -> int                                 # PRAGMA user_version; migrations = ordered list

    # events
    def add_event(self, type: EventType, source: Source | None = None, treats: int = 0,
                  detail: str | None = None, ts: datetime | None = None) -> Event
    def events_between(self, start_utc, end_utc, types: Collection[EventType] | None = None) -> list[Event]  # ascending ts
    def last_event(self, types: Collection[EventType] | EventType) -> Event | None
    def treats_between(self, start_utc, end_utc) -> int   # SUM(treats) of DISPENSE events in [start, end)
    # schedules
    def add_schedule(self, time_local: str, count: int = 1, jitter_min: int = 0, created_by: int | None = None) -> Schedule
    def list_schedules(self, enabled_only: bool = False) -> list[Schedule]   # ordered by time_local, id
    def remove_schedule(self, schedule_id: int) -> bool
    def set_schedule_enabled(self, schedule_id: int, enabled: bool) -> bool
    # settings
    def get_setting(self, key: str, default: str | None = None) -> str | None
    def set_setting(self, key: str, value: str) -> None
    # level readings (30-day retention)
    def add_level_reading(self, ts: datetime, distance_mm: int, pct: float | None) -> None
    def level_readings_since(self, since_utc) -> list[tuple[datetime, int, float | None]]
    def prune_level_readings(self, older_than_utc) -> int
```
Schema exactly as PRD §3.9 (`pct` nullable). `time_local` validated `HH:MM`.

### core/policy.py (M1-4)

```python
class DailyCapPolicy:
    def __init__(self, counter: TreatCounter, tz: ZoneInfo, cap: int, now: NowFn) -> None
    cap: int
    def day_bounds(self, at: datetime | None = None) -> tuple[datetime, datetime]   # [local midnight, next local midnight) in UTC; DST-safe
    def local_date(self, at: datetime | None = None) -> date
    def treats_today(self) -> int
    def remaining(self) -> int                       # max(cap - treats_today, 0)
    def allows(self, n: int = 1) -> bool             # treats_today + n <= cap
```

### core/clock.py (M1-7)

```python
class ClockGuard:
    def __init__(self, now: NowFn, *, ntp_synced: Callable[[], bool] | None = None,
                 rtc_present: Callable[[], bool] | None = None, min_year: int = 2026) -> None
        # defaults: ntp_synced -> `timedatectl show -p NTPSynchronized --value` == "yes" (False if unavailable);
        #           rtc_present -> /dev/rtc0 exists AND `hwclock`-independent: treat presence as trusted (PRD A7).
    def is_trustworthy(self) -> bool        # (ntp_synced() or rtc_present()) and now().year >= min_year
    def reason(self) -> str                 # human text for the skip alert
```

### core/feeder.py (M1-5)

```python
class FeederState(StrEnum): READY="ready"; EMPTY_OR_JAMMED="empty_or_jammed"

class Feeder:
    def __init__(self, motor: Motor, drop_sensor: DropSensor, policy: DailyCapPolicy, db: Database,
                 alerts: Alerts, now: NowFn, *, drop_timeout_s: float = 1.5,
                 before_dispense: Callable[[], Awaitable[None]] | None = None) -> None
    state: FeederState
    async def dispense(self, source: Source) -> DispenseResult
    def clear_jam(self) -> None             # called on refill
    on_state_change: Callable[[FeederState], None] | None   # optional hook
```
Algorithm (PRD §3.4): serialised by `asyncio.Lock`; cap check -> log `cap_reached` + `CAP_REACHED`;
jammed + source SCHEDULE -> `EMPTY_OR_JAMMED` (manual sources may retry; success clears the state);
`before_dispense()` (level reading); arm, advance, wait drop; none -> one more pocket; still none ->
`dispense_failed` + `jam` events, state JAMMED, `alerts.push`, result `EMPTY_OR_JAMMED`; success ->
`dispense` event (`treats` = drops counted), motor released, and `alerts.silent(...)` for SCHEDULE/BUTTON sources.
Failed attempts never count toward the cap.

### core/supply.py (M1-6)

```python
class SupplyMonitor:
    def __init__(self, sensor: LevelSensor, db: Database, alerts: Alerts, cfg: SupplyConfig,
                 capacity_treats: int, now: NowFn, *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 samples: int = 5, refill_confirm_delay_s: float = 30.0) -> None
    def distance_to_pct(self, distance_mm: float) -> float | None       # clamp 0..100; None if uncalibrated
    async def read(self) -> SupplyStatus          # median of `samples` reads; stores level_reading; runs state machine
    def status(self) -> SupplyStatus              # last known (no hardware access)
    async def run(self) -> None                   # loop: read() every cfg.poll_interval_s; cancellable
    def add_refill_listener(self, cb: Callable[[], None]) -> None
```
Low alert: `alerts.push` + `low_supply` event once when pct <= low_threshold; re-armed when pct >= low_clear or refill.
Refill: pct rose >= refill_rise_pct vs previous reading, confirmed by a second read after
`refill_confirm_delay_s` still >= that rise -> `refill` event, `alerts.silent("Refill detected ...")`, re-arm, call listeners.
`days_left` = treats_left / (treats dispensed in last 7 days / 7), None if no consumption.
Uncalibrated (empty_mm/full_mm None): `read()` returns pct=None status and does no alerting.

### adapters/leds.py (M1-9)

```python
class PowerState(StrEnum): BOOTING; RUNNING; SHUTTING_DOWN; OFF
def compute_led_states(power: PowerState, connected: ConnState, supply_pct: float | None, jammed: bool,
                       low_threshold_pct: float) -> dict[Led, LedMode]      # pure, table-tested
class LedController:
    def __init__(self, leds: StatusLeds, brightness: float, now: NowFn, *, supply_pct: Callable[[], float | None],
                 is_jammed: Callable[[], bool], low_threshold_pct: float) -> None
    def set_power(self, state: PowerState) -> None
    def mark_poll_ok(self) -> None; def mark_poll_failed(self) -> None
    def refresh(self) -> None                     # recompute + apply to hardware
    async def run(self, interval_s: float = 1.0) -> None
```
Connected: SOLID if last OK < 60 s ago; BLINK while reconnecting (failure < 5 min); OFF when offline > 5 min.

### notify.py (M1-11)

```python
MessageSender = Callable[[int, str, bool], Awaitable[None]]    # (chat_id, text, silent); raises on failure
class Notifier:                                                  # implements Alerts
    def __init__(self, send: MessageSender, chat_ids: Sequence[int], db: Database, now: NowFn, *, max_queue: int = 100)
    async def push(self, text: str) -> None
    async def silent(self, text: str) -> None
    async def set_connectivity(self, ok: bool) -> None          # heartbeat result from the bot
    online: bool; queue_size: int
    async def flush(self) -> None
def build_daily_summary(policy: DailyCapPolicy, db: Database, supply: SupplyStatus, tz: ZoneInfo, now: NowFn) -> str
```
A failed send queues the message (bounded, oldest dropped) and records `offline`; the next success (or
`set_connectivity(True)`) records `online`, flushes the queue in order and sends a push
"Back online (down for 12 min)". Never raises into callers.

### adapters/scheduler.py (M1-8)

```python
class FeedScheduler:
    def __init__(self, db: Database, feeder: Feeder, clock: ClockGuard, alerts: Alerts, tz: ZoneInfo, now: NowFn,
                 *, daily_summary: Callable[[], Awaitable[None]] | None = None, summary_time: time | None = None,
                 sleep=asyncio.sleep, rng: random.Random | None = None) -> None
    def start(self) -> None; def shutdown(self) -> None
    def reload(self) -> None                       # rebuild jobs from enabled DB schedules (+ summary job)
    async def run_slot(self, schedule: Schedule) -> None    # the testable job body
```
`run_slot`: clock untrusted -> `schedule_skipped` event + push; else dispense `count` times via
`Feeder.dispense(Source.SCHEDULE)`; stop at first non-OK, log `schedule_skipped` + push (cap / jam). No catch-up
(APScheduler `misfire_grace_time=60`, `coalesce=True`). Jitter ±N min: cron fires at slot−N, sleeps uniform(0, 2N) min.

### adapters/telegram_bot.py (M1-10)

```python
@dataclass class BotDeps: feeder; policy; db; supply_status: Callable[[], SupplyStatus]; reload_schedules: Callable[[], None]
                          tz; now; started_at; default_jitter_min; allowed_user_ids
class TelegramBot:
    def __init__(self, token: str, deps: BotDeps, *, on_connectivity: Callable[[bool], Awaitable[None] | None] | None = None,
                 heartbeat_s: float = 30.0)
    async def start(self) -> None; async def stop(self) -> None        # long polling + heartbeat (bot.get_me)
    async def send(self, chat_id: int, text: str, silent: bool) -> None    # == MessageSender
```
Commands `/start /help /treat /status /today /last /schedule add|list|remove`; inline buttons [🐟 Treat] [📊 Status] [📅 Today].
Allow-list enforced before any handler; strangers get **no reply**, an `unauthorized` event is stored.
`/treat` replies "Treat dispensed ✅ (3/8 today)" or the refusal reason. Handlers are plain async functions
tested with mocked `Update`/`Context`.

### app.py (M1-12)

```python
class FeederApp:
    @classmethod def build(cls, config: Config, hardware: FakeHardware|HAL bundle, *, sender: MessageSender | None = None,
                           now: NowFn | None = None) -> "FeederApp"      # wires everything
    async def start(self) -> None; async def stop(self) -> None
def configure_logging(level: str = "INFO") -> None
async def main(argv=None) -> int ; def cli() -> None      # `python -m feeder --config config.yaml --fake`
```
`adapters/buttons.py`: `ButtonAdapter(buttons, feeder, loop)` — treat press -> `Feeder.dispense(Source.BUTTON)`.

### deploy (M1-13)
`deploy/pet-treat-feeder.service`, `deploy/install.sh`, `Makefile` target `deploy` (rsync + `systemctl restart` over SSH,
`PI_HOST` variable).

## Dependency graph

```
models, ports ──► everything
config ─────────► supply, leds(brightness), scheduler(jitter), bot, app
hardware(base/fake) ─► feeder, supply, leds, buttons, app
storage ─► policy(TreatCounter only), feeder, supply, notify, scheduler, bot, app
policy ─► feeder, notify(summary), bot
clock ─► scheduler
feeder ─► scheduler, bot, buttons
supply ─► feeder (before_dispense hook, refill->clear_jam in app), leds (pct provider), bot (/status), notify (summary)
notify ─► (Alerts) feeder, supply, scheduler;  bot.send ─► notify
all ─► app ─► deploy
```

## Build waves

| Wave | Components | Notes |
|---|---|---|
| 0 | scaffold, CI, `models.py`, `ports.py`, this plan (done by lead) | one shared layout |
| 1 | M1-1 HAL+fakes, M1-2 config, M1-3 storage, M1-4 cap policy, M1-7 clock guard | independent; policy uses `TreatCounter` stub in tests |
| 2 | M1-5 feeder, M1-6 supply monitor, M1-9 LED state machine | need wave 1 |
| 3 | M1-8 scheduler, M1-11 notifier, M1-10 telegram bot | need wave 2; bot/scheduler use stubs for each other |
| 4 | M1-12 app wiring (+ buttons adapter), M1-13 deploy | integration |

Each wave ends with the lead running `ruff check .` + `pytest`, fixing interface mismatches, and one commit per component.

## Deviations / decisions (carried into the PR)

1. PRD §3.4 step 2 says to reject while `EMPTY_OR_JAMMED`, yet "cleared by a successful manual retry". Implemented as:
   scheduled dispenses are rejected while jammed; manual sources (Telegram/button) may retry and clear it on success.
2. Jitter is true ±N min (cron fires at slot−N, then random delay 0..2N).
3. Daily summary job is owned by the scheduler (APScheduler) but its content by `notify.build_daily_summary`.
