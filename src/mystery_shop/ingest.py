"""Ingest restaurant leads from a spreadsheet into SQLite.

Supports both .xlsx and .csv inputs (the brief mentions CSV first). The file format
is auto-detected from the extension. Both paths feed a shared `_ingest_rows()` so
dedup, COALESCE, and contact-linking logic only exists in one place.

Dedupes by phone number (same restaurant can have many owners). Every owner is kept
linked through lead_contacts so an SDR can see who works there."""
from __future__ import annotations

import csv
import logging
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import urlparse

import openpyxl

from .db import transaction
from .timezones import state_to_timezone

logger = logging.getLogger(__name__)


def normalize_phone(raw: Any) -> str | None:
    """Return E.164-ish digits with leading +. Drops anything that doesn't look like a phone."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) < 11 or len(digits) > 15:
        return None
    return "+" + digits


def derive_name_from_website(url: str | None) -> str | None:
    """Best-effort restaurant name from a domain. 'http://www.313franklin.com' -> '313 Franklin'."""
    if not url:
        return None
    try:
        host = urlparse(url if "://" in url else "http://" + url).hostname or ""
    except Exception:
        return None
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    base = host.split(".")[0] if host else ""
    if not base:
        return None
    parts = re.findall(r"[A-Za-z][a-z']*|\d+", base)
    if not parts:
        return base
    return " ".join(p.capitalize() for p in parts)


def _coerce_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _coerce_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


# --------------------------- format-specific readers ---------------------------

def _read_xlsx(path: Path) -> Iterator[dict[str, Any]]:
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    header = [str(h or "").strip() for h in next(rows)]
    for row in rows:
        yield {header[i]: row[i] for i in range(min(len(header), len(row)))}


def _read_csv(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        yield from reader


def _read_any(path: Path) -> Iterator[dict[str, Any]]:
    ext = path.suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        return _read_xlsx(path)
    if ext in (".csv", ".tsv"):
        return _read_csv(path)
    raise ValueError(f"Unsupported file extension: {ext} (expected .xlsx, .xlsm, .csv, .tsv)")


# --------------------------- core ingest ---------------------------

def _ingest_rows(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Process an iterable of row dicts. Returns counters + skip_reasons breakdown."""
    counters: dict[str, Any] = {
        "leads_inserted": 0, "leads_updated": 0,
        "contacts_inserted": 0, "skipped": 0,
    }
    skip_reasons: Counter[str] = Counter()

    with transaction(conn):
        for row in rows:
            raw_phone = row.get("Location Phone")
            phone = normalize_phone(raw_phone)
            if not phone:
                counters["skipped"] += 1
                if raw_phone is None or _coerce_str(raw_phone) is None:
                    skip_reasons["empty_phone"] += 1
                else:
                    skip_reasons["invalid_phone"] += 1
                continue

            website = _coerce_str(row.get("organization_website_url"))
            state = _coerce_str(row.get("organization_state"))
            lead_payload = {
                "phone": phone,
                "restaurant_name": derive_name_from_website(website),
                "website": website,
                "street_address": _coerce_str(row.get("organization_street_address")),
                "raw_address": _coerce_str(row.get("organization_raw_address")),
                "city": _coerce_str(row.get("organization_city")),
                "state": state,
                "country": _coerce_str(row.get("organization_country")),
                "postal_code": _coerce_str(row.get("organization_postal_code")),
                "google_reviews_count": _coerce_int(row.get("Google Reviews Count")),
                "google_maps_url": _coerce_str(row.get("Google Maps Url")),
                "timezone": state_to_timezone(state),
            }

            existing = conn.execute("SELECT id FROM leads WHERE phone = ?", (phone,)).fetchone()
            if existing:
                lead_id = existing["id"]
                conn.execute(
                    """UPDATE leads SET
                        restaurant_name = COALESCE(restaurant_name, :restaurant_name),
                        website = COALESCE(website, :website),
                        street_address = COALESCE(street_address, :street_address),
                        raw_address = COALESCE(raw_address, :raw_address),
                        city = COALESCE(city, :city),
                        state = COALESCE(state, :state),
                        country = COALESCE(country, :country),
                        postal_code = COALESCE(postal_code, :postal_code),
                        google_reviews_count = COALESCE(google_reviews_count, :google_reviews_count),
                        google_maps_url = COALESCE(google_maps_url, :google_maps_url),
                        timezone = COALESCE(timezone, :timezone)
                       WHERE id = :id""",
                    {**lead_payload, "id": lead_id},
                )
                counters["leads_updated"] += 1
            else:
                cur = conn.execute(
                    """INSERT INTO leads
                        (phone, restaurant_name, website, street_address, raw_address,
                         city, state, country, postal_code, google_reviews_count,
                         google_maps_url, timezone)
                       VALUES
                        (:phone, :restaurant_name, :website, :street_address, :raw_address,
                         :city, :state, :country, :postal_code, :google_reviews_count,
                         :google_maps_url, :timezone)""",
                    lead_payload,
                )
                lead_id = cur.lastrowid
                counters["leads_inserted"] += 1

            first = _coerce_str(row.get("first_name"))
            last = _coerce_str(row.get("last_name"))
            email = _coerce_str(row.get("email"))
            if first or last or email:
                existing_contact = conn.execute(
                    """SELECT id FROM lead_contacts
                       WHERE lead_id = ? AND (
                           (email IS NOT NULL AND email = ?) OR
                           (email IS NULL AND ? IS NULL AND first_name IS ? AND last_name IS ?)
                       )""",
                    (lead_id, email, email, first, last),
                ).fetchone()
                if not existing_contact:
                    conn.execute(
                        "INSERT INTO lead_contacts (lead_id, first_name, last_name, email) VALUES (?, ?, ?, ?)",
                        (lead_id, first, last, email),
                    )
                    counters["contacts_inserted"] += 1

    counters["skip_reasons"] = dict(skip_reasons)
    if counters["skipped"]:
        logger.info("ingest: skipped %d rows: %s", counters["skipped"], counters["skip_reasons"])
    return counters


def ingest_file(conn: sqlite3.Connection, path: Path) -> dict[str, Any]:
    """Entry point. Auto-detects format from the file extension."""
    return _ingest_rows(conn, _read_any(path))


# Back-compat: existing tests import ingest_xlsx.
def ingest_xlsx(conn: sqlite3.Connection, path: Path) -> dict[str, Any]:
    return ingest_file(conn, path)
