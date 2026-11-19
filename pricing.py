"""Pure dated price resolution. No portfolio identities, files, or provider calls."""
from bisect import bisect_right
from dataclasses import dataclass, asdict
from datetime import date, datetime
from decimal import Decimal
from collections import defaultdict

PRIORITY = {'legacy': 0, 'execution': 1, 'manual': 2, 'intraday': 3, 'close': 4}


@dataclass(frozen=True)
class Observation:
    instrument: str
    effective_date: date
    value: Decimal
    source: str
    kind: str
    reference: str
    order: int = 0
    received_at: str | None = None
    recorded_at: str | None = None

    def __post_init__(self):
        if not self.value.is_finite() or self.value <= 0:
            raise ValueError('Prices must be finite and positive')
        if self.kind not in PRIORITY:
            raise ValueError('Unknown price kind')


@dataclass(frozen=True)
class PriceResult:
    value: Decimal | None
    effective_date: date | None
    source: str | None
    kind: str | None
    reference: str | None
    carried_forward: bool
    status: str
    reason: str | None = None
    received_at: str | None = None
    recorded_at: str | None = None
    split_factor: str = "1"
    original_value: str | None = None

    def to_dict(self):
        result = asdict(self)
        result['value'] = float(self.value) if self.value is not None else None
        result['effective_date'] = self.effective_date.isoformat() if self.effective_date else None
        return result


class PriceBook:
    def __init__(self, observations=(), modes=None, splits=(), generation=None):
        self.modes = dict(modes or {})
        self.generation = generation
        self.splits = tuple(splits)
        grouped = defaultdict(list)
        for obs in observations:
            grouped[obs.instrument].append(obs)
        self.observations = {
            ticker: sorted(rows, key=lambda o: (o.effective_date, PRIORITY[o.kind], o.order, o.reference))
            for ticker, rows in grouped.items()
        }
        # Unknown adjustment basis is preserved for audit, never silently valued.
        self.eligible = {t: [o for o in rows if o.kind != "legacy"] for t, rows in self.observations.items()}
        self.dates = {t: [o.effective_date for o in rows] for t, rows in self.eligible.items()}

    @property
    def latest_date(self):
        return max((ds[-1] for ds in self.dates.values() if ds), default=None)

    def resolve(self, ticker, at, purpose='valuation'):
        if isinstance(at, datetime):
            raise ValueError('This daily-price book supports end-of-day dates, not intraday timestamps')
        if not isinstance(at, date):
            at = date.fromisoformat(str(at)[:10])
        if purpose not in {'valuation', 'session_close'}:
            raise ValueError('Unknown price purpose')
        rows = self.eligible.get(ticker, ())
        end = bisect_right(self.dates.get(ticker, ()), at)
        if purpose == 'session_close':
            candidates = [o for o in rows[:end] if o.effective_date == at and o.kind == 'close']
            obs = candidates[-1] if candidates else None
        else:
            obs = rows[end - 1] if end else None
        if obs is None:
            return PriceResult(None, None, None, None, None, False, 'unavailable',
                               'No eligible price at or before the valuation date' if purpose == 'valuation'
                               else 'No official close for this session')
        value = obs.value
        factor = Decimal("1")
        for split_date, instrument, ratio in self.splits:
            if instrument == ticker and obs.effective_date < split_date <= at:
                value /= ratio
                factor *= ratio
        carried = obs.effective_date < at
        status = 'static' if self.modes.get(ticker) == 'static' else ('carried' if carried else 'available')
        return PriceResult(value, obs.effective_date, obs.source, obs.kind, obs.reference,
                           carried, status, received_at=obs.received_at, recorded_at=obs.recorded_at,
                           split_factor=str(factor), original_value=str(obs.value))
