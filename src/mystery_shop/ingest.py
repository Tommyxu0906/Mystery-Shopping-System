"""Ingest the lead spreadsheet into SQLite.

Dedupes by phone number (same restaurant can have many owners). Keeps every owner
linked back through lead_contacts so an SDR can see the relationship side."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import openpyxl

from .db import transaction
from .timezones import state_to_timezone


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
    # Split camelCase and digit/letter boundaries; title-case the result.
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


def ingest_xlsx(conn: sqlite3.Connection, xlsx_path: Path) -> dict[str, int]:
    """Ingest every row. Returns counters {leads_inserted, leads_updated, contacts_inserted, skipped}."""
    wb = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    ws = wb.active

    rows = ws.iter_rows(values_only=True)
    header = [str(h or "").strip() for h in next(rows)]
    col = {name: i for i, name in enumerate(header)}

    def get(row: tuple, name: str) -> Any:
        idx = col.get(name)
        return row[idx] if idx is not None and idx < len(row) else None

    counters = {"leads_inserted": 0, "leads_updated": 0, "contacts_inserted": 0, "skipped": 0}

    with transaction(conn):
        for row in rows:
            phone = normalize_phone(get(row, "Location Phone"))
            if not phone:
                counters["skipped"] += 1
                continue

            website = _coerce_str(get(row, "organization_website_url"))
            state = _coerce_str(get(row, "organization_state"))
            lead_payload = {
                "phone": phone,
                "restaurant_name": derive_name_from_website(website),
                "website": website,
                "street_address": _coerce_str(get(row, "organization_street_address")),
                "raw_address": _coerce_str(get(row, "organization_raw_address")),
                "city": _coerce_str(get(row, "organization_city")),
                "state": state,
                "country": _coerce_str(get(row, "organization_country")),
                "postal_code": _coerce_str(get(row, "organization_postal_code")),
                "google_reviews_count": _coerce_int(get(row, "Google Reviews Count")),
                "google_maps_url": _coerce_str(get(row, "Google Maps Url")),
                "timezone": state_to_timezone(state),
            }

            existing = conn.execute("SELECT id FROM leads WHERE phone = ?", (phone,)).fetchone()
            if existing:
                lead_id = existing["id"]
                # Fill in any missing fields without clobbering good data.
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

            first = _coerce_str(get(row, "first_name"))
            last = _coerce_str(get(row, "last_name"))
            email = _coerce_str(get(row, "email"))
            if first or last or email:
                # Dedupe contact by (lead_id, email) or (lead_id, first, last) — emails win when present.
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

    return counters
