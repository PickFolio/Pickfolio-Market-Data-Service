from __future__ import annotations

import math
import re
from dataclasses import dataclass


IMPACT_METHOD = "impact_v1"


@dataclass(frozen=True)
class ImpactScore:
    score: float
    method: str = IMPACT_METHOD
    event_type: str = "GENERAL_NEWS"


@dataclass(frozen=True)
class EventRule:
    event_type: str
    base_score: float
    patterns: tuple[str, ...]


EVENT_RULES: tuple[EventRule, ...] = (
    EventRule("EARNINGS_BEAT", 0.68, (r"\bbeats?\b.*\b(estimates?|expectations?|street|profit|revenue|sales)\b", r"\b(profit|revenue|sales|ebitda|pat)\b.*\bbeats?\b", r"\b(strong|record|robust)\b.*\b(results?|earnings|profit|revenue|sales)\b", r"\bprofit\b.*\b(rises?|jumps?|surges?|soars?|beats?)\b")),
    EventRule("EARNINGS_MISS", -0.68, (r"\bmiss(es|ed)?\b.*\b(estimates?|expectations?|street|profit|revenue|sales)\b", r"\b(profit|revenue|sales|ebitda|pat)\b.*\bmiss(es|ed)?\b", r"\bprofit\b.*\b(falls?|drops?|declines?|slumps?|plunges?)\b", r"\b(weak|disappointing|muted)\b.*\b(results?|earnings|profit|revenue|sales)\b")),
    EventRule("ORDER_WIN", 0.62, (r"\b(wins?|bags?|secures?|receives?|gets|lands)\b.*\b(order|contract|project|deal|tender)\b", r"\b(order|contract|project|deal|tender)\b.*\b(won|win|secured|bagged|awarded|received)\b", r"\b(loi|letter of intent)\b")),
    EventRule("ORDER_LOSS", -0.58, (r"\b(loses?|lost)\b.*\b(order|contract|project|deal|tender)\b", r"\b(order|contract|project|deal|tender)\b.*\b(cancelled|canceled|terminated|scrapped)\b")),
    EventRule("M_AND_A", 0.42, (r"\b(acquires?|acquisition|merger|merge|amalgamation|takeover|stake purchase|buys? stake)\b", r"\b(to buy|will buy|set to buy)\b.*\bstake\b")),
    EventRule("REGULATORY_APPROVAL", 0.58, (r"\b(approval|approved|clearance|cleared|nod|license|licence)\b.*\b(sebi|rbi|cci|usfda|fda|dcgi|regulator|ministry|board)\b", r"\b(sebi|rbi|cci|usfda|fda|dcgi|regulator|ministry|board)\b.*\b(approval|approved|clearance|cleared|nod|license|licence)\b")),
    EventRule("REGULATORY_ACTION", -0.68, (r"\b(probe|investigation|notice|warning letter|penalty|fine|ban|raid|searches|summons|show cause)\b", r"\b(sebi|rbi|cci|usfda|fda|ed|income tax|tax department|regulator)\b.*\b(action|probe|notice|penalty|fine|ban)\b")),
    EventRule("ANALYST_UPGRADE", 0.45, (r"\b(upgrades?|raises?|hikes?)\b.*\b(rating|target price|price target|tp)\b", r"\b(buy|outperform|overweight|accumulate)\b.*\b(rating|call)\b")),
    EventRule("ANALYST_DOWNGRADE", -0.45, (r"\b(downgrades?|cuts?|lowers?|reduces?)\b.*\b(rating|target price|price target|tp)\b", r"\b(sell|underperform|underweight|reduce)\b.*\b(rating|call)\b")),
    EventRule("MANAGEMENT_EXIT", -0.48, (r"\b(resigns?|steps down|quits?|exit|leaves?)\b.*\b(ceo|cfo|coo|md|chairman|director|auditor|promoter|founder)\b", r"\b(ceo|cfo|coo|md|chairman|director|auditor|promoter|founder)\b.*\b(resigns?|steps down|quits?|exit|leaves?)\b")),
    EventRule("MANAGEMENT_APPOINTMENT", 0.18, (r"\b(appoints?|names?)\b.*\b(ceo|cfo|coo|md|chairman|director|auditor|promoter|founder)\b", r"\b(ceo|cfo|coo|md|chairman|director)\b.*\b(appointed|named)\b")),
    EventRule("CAPITAL_RAISE", 0.22, (r"\b(raises?|fundraise|fund raising|funding|qip|rights issue|preferential issue)\b",)),
    EventRule("DEBT_STRESS", -0.62, (r"\b(default|insolvency|bankruptcy|nclt|debt restructuring|loan default|rating downgrade)\b",)),
    EventRule("GUIDANCE_RAISE", 0.55, (r"\b(raises?|hikes?|increases?)\b.*\b(guidance|forecast|outlook)\b", r"\b(guidance|forecast|outlook)\b.*\b(raised|hiked|increased|stronger)\b")),
    EventRule("GUIDANCE_CUT", -0.55, (r"\b(cuts?|lowers?|reduces?)\b.*\b(guidance|forecast|outlook)\b", r"\b(guidance|forecast|outlook)\b.*\b(cut|lowered|reduced|weaker)\b")),
)

POSITIVE_TERMS = ("beat", "beats", "strong", "record", "robust", "surge", "surges", "jump", "jumps", "rises", "gains", "wins", "secures", "bags", "approved", "approval", "upgrade", "upgrades", "buy", "outperform", "raises", "hikes")
NEGATIVE_TERMS = ("miss", "misses", "weak", "fall", "falls", "drop", "drops", "declines", "slump", "slumps", "loss", "probe", "penalty", "fine", "ban", "raid", "default", "downgrade", "downgrades", "sell", "underperform", "cuts", "lowers", "resigns")

MAGNITUDE_RE = re.compile(
    r"(?P<currency>rs\.?|inr|\u20b9|\$|usd)?\s*(?P<amount>\d+(?:,\d{2,3})*(?:\.\d+)?)\s*(?P<unit>crores?|cr|lakh|lakhs|million|mn|billion|bn)?",
    re.IGNORECASE,
)


def score_market_impact(headline: str | None) -> ImpactScore:
    text = " ".join((headline or "").lower().split())
    if not text:
        return ImpactScore(score=0.0)

    event_type, base_score = _classify_event(text)
    tone_score = _tone_score(text)
    raw_score = tone_score * 0.45 if base_score == 0.0 else base_score + tone_score * 0.18
    magnitude_boost = _magnitude_boost(text)
    if magnitude_boost and raw_score:
        raw_score += math.copysign(magnitude_boost, raw_score)
    return ImpactScore(score=_clamp(round(raw_score, 3)), event_type=event_type)


def _classify_event(text: str) -> tuple[str, float]:
    for rule in EVENT_RULES:
        if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in rule.patterns):
            return rule.event_type, rule.base_score
    return "GENERAL_NEWS", 0.0


def _tone_score(text: str) -> float:
    positive = sum(1 for term in POSITIVE_TERMS if re.search(rf"\b{re.escape(term)}\b", text))
    negative = sum(1 for term in NEGATIVE_TERMS if re.search(rf"\b{re.escape(term)}\b", text))
    if positive == negative:
        return 0.0
    return _clamp((positive - negative) / 4.0)


def _magnitude_boost(text: str) -> float:
    largest_crore = 0.0
    for match in MAGNITUDE_RE.finditer(text):
        currency = (match.group("currency") or "").lower()
        unit = (match.group("unit") or "").lower()
        if not currency and not unit:
            continue
        try:
            amount = float(match.group("amount").replace(",", ""))
        except ValueError:
            continue
        largest_crore = max(largest_crore, _to_crore(amount, currency, unit))
    if largest_crore >= 5000:
        return 0.18
    if largest_crore >= 1000:
        return 0.14
    if largest_crore >= 500:
        return 0.10
    if largest_crore >= 100:
        return 0.06
    if largest_crore >= 25:
        return 0.03
    return 0.0


def _to_crore(amount: float, currency: str, unit: str) -> float:
    if unit in {"crore", "crores", "cr"}:
        return amount
    if unit in {"lakh", "lakhs"}:
        return amount / 100.0
    if unit in {"million", "mn"}:
        return amount * (8.3 if currency in {"$", "usd"} else 0.1)
    if unit in {"billion", "bn"}:
        return amount * (830.0 if currency in {"$", "usd"} else 100.0)
    if currency in {"rs", "rs.", "inr", "\u20b9"}:
        return amount / 10_000_000.0
    if currency in {"$", "usd"}:
        return amount * 8.3 / 1_000_000.0
    return 0.0


def _clamp(value: float) -> float:
    return max(-1.0, min(1.0, value))
