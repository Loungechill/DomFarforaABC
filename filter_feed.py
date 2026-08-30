#!/usr/bin/env python3
"""Build the Dom Farfora vendor-filtered YML feed with ABC labels."""

import argparse
import csv
import html
import io
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo


DEFAULT_SOURCE = (
    "https://api.domfarfora.ru/bitrix/catalog_export/yandex-direct.xml"
)
DEFAULT_STATS_SOURCE = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vRGIY-NLZaOAoB_4tqJYxtdMX6b5zgNrBeIBj3j-pIW3imbTTVijTHuGyrgO3OiKJkyE2EYWRda1Tt4/"
    "pub?gid=0&single=true&output=csv"
)
CUSTOM_LABEL_TAG = "custom_label_0"
ALLOWED_CUSTOM_LABELS = {"1", "2", "3"}
NAME_HEADER_ALIASES = (
    "название товара или каталога",
    "название товара",
    "product name",
    "name",
)
PURCHASE_HEADER_ALIASES = (
    "конверсии",
    "покупки",
    "conversions",
    "purchases",
)


def normalize_text(value: str | None) -> str:
    value = html.unescape(value or "")
    value = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_name(value: str | None) -> str:
    return normalize_text(value).casefold()


def normalize_vendor(value: str | None) -> str:
    return normalize_name(value)


def normalize_header(value: str | None) -> str:
    return normalize_name(value).replace("_", " ")


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def qualified_tag(reference_tag: str, name: str) -> str:
    if reference_tag.startswith("{") and "}" in reference_tag:
        namespace = reference_tag.split("}", 1)[0] + "}"
        return f"{namespace}{name}"
    return name


def find_child_by_local_name(
    parent: ET.Element,
    name: str,
) -> ET.Element | None:
    for child in parent:
        if local_name(child.tag) == name:
            return child
    return None


def parse_purchases(value: str | None, row_number: int) -> int:
    normalized = normalize_text(value).replace(" ", "").replace(",", ".")
    if not normalized:
        return 0

    try:
        parsed = Decimal(normalized)
    except InvalidOperation as exc:
        raise RuntimeError(
            f"Invalid conversions value at CSV row {row_number}: {value!r}"
        ) from exc

    if parsed < 0 or parsed != parsed.to_integral_value():
        raise RuntimeError(
            "Conversions must be a non-negative integer at CSV row "
            f"{row_number}: {value!r}"
        )
    return int(parsed)


def _resolve_header(
    positions: dict[str, int],
    aliases: tuple[str, ...],
) -> int | None:
    return next((positions[name] for name in aliases if name in positions), None)


def load_purchase_stats(path: Path) -> tuple[dict[str, int], dict[str, int]]:
    raw_bytes = path.read_bytes()
    if len(raw_bytes) < 100:
        raise RuntimeError(
            f"Purchase CSV is unexpectedly small: {len(raw_bytes)} bytes"
        )

    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RuntimeError("Purchase CSV must use UTF-8 encoding") from exc

    delimiter = None
    for candidate in (",", ";", "\t"):
        candidate_reader = csv.reader(
            io.StringIO(text, newline=""),
            delimiter=candidate,
            quotechar='"',
            doublequote=True,
        )
        try:
            candidate_headers = next(candidate_reader)
        except StopIteration:
            continue
        candidate_normalized = {
            normalize_header(header) for header in candidate_headers
        }
        if (
            any(name in candidate_normalized for name in NAME_HEADER_ALIASES)
            and any(
                name in candidate_normalized
                for name in PURCHASE_HEADER_ALIASES
            )
        ):
            delimiter = candidate
            break

    if delimiter is None:
        raise RuntimeError(
            "Could not determine the CSV delimiter or required columns"
        )

    reader = csv.reader(
        io.StringIO(text, newline=""),
        delimiter=delimiter,
        quotechar='"',
        doublequote=True,
    )
    try:
        headers = next(reader)
    except StopIteration as exc:
        raise RuntimeError("Purchase CSV is empty") from exc

    positions: dict[str, int] = {}
    for index, header in enumerate(headers):
        normalized = normalize_header(header)
        if normalized in positions:
            raise RuntimeError(
                f"Duplicate CSV header after normalization: {header!r}"
            )
        positions[normalized] = index

    name_index = _resolve_header(positions, NAME_HEADER_ALIASES)
    purchases_index = _resolve_header(positions, PURCHASE_HEADER_ALIASES)
    if name_index is None or purchases_index is None:
        raise RuntimeError(
            "Purchase CSV must contain product-name and conversions columns; "
            f"found: {headers}"
        )

    purchase_counts: dict[str, int] = {}
    nonempty_rows = 0
    blank_name_rows = 0
    duplicate_rows_aggregated = 0
    total_conversions = 0

    for row_number, row in enumerate(reader, start=2):
        if not any(normalize_text(cell) for cell in row):
            continue

        nonempty_rows += 1
        product_name = normalize_name(
            row[name_index] if name_index < len(row) else None
        )
        if not product_name:
            blank_name_rows += 1
            continue

        purchases = parse_purchases(
            row[purchases_index] if purchases_index < len(row) else None,
            row_number,
        )
        if product_name in purchase_counts:
            duplicate_rows_aggregated += 1
        purchase_counts[product_name] = purchase_counts.get(product_name, 0) + purchases
        total_conversions += purchases

    if not purchase_counts:
        raise RuntimeError("Purchase CSV contains no usable product names")

    return purchase_counts, {
        "purchase_csv_rows": nonempty_rows,
        "purchase_csv_unique_names": len(purchase_counts),
        "purchase_csv_blank_name_rows": blank_name_rows,
        "purchase_csv_duplicate_rows_aggregated": duplicate_rows_aggregated,
        "purchase_csv_total_conversions": total_conversions,
        "purchase_csv_bytes": len(raw_bytes),
    }


def load_blacklist(path: Path) -> tuple[list[str], set[str]]:
    raw = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    normalized = {normalize_vendor(value) for value in raw}
    if not raw:
        raise RuntimeError("Blacklist is empty")
    if len(raw) != len(normalized):
        raise RuntimeError("Blacklist contains duplicates after normalization")
    return raw, normalized


def download_http_with_retries(
    source: str,
    destination: Path,
    headers: dict[str, str],
    description: str,
    timeout: int,
    attempts: int = 3,
) -> None:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(source, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if response.status != 200:
                    raise RuntimeError(
                        f"{description} returned HTTP {response.status}"
                    )
                with destination.open("wb") as output:
                    shutil.copyfileobj(response, output)
            return
        except (TimeoutError, urllib.error.URLError, OSError, RuntimeError) as exc:
            last_error = exc
            if destination.exists():
                destination.unlink()
            if attempt < attempts:
                wait_seconds = attempt * 10
                print(
                    f"WARNING: {description} download attempt {attempt}/{attempts} "
                    f"failed: {exc}; retrying in {wait_seconds}s",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(wait_seconds)

    raise RuntimeError(
        f"{description} download failed after {attempts} attempts: {last_error}"
    )


def download_source(source: str, destination: Path) -> None:
    if source.startswith(("http://", "https://")):
        download_http_with_retries(
            source,
            destination,
            headers={
                "User-Agent": "DomFarfora-ABC-Feed/1.0 (+GitHub Actions)",
                "Accept": "application/xml,text/xml;q=0.9,*/*;q=0.8",
            },
            description="Source feed",
            timeout=300,
        )
    else:
        shutil.copyfile(source, destination)

    size = destination.stat().st_size
    if size < 1_000_000:
        raise RuntimeError(f"Downloaded source is unexpectedly small: {size} bytes")


def download_purchase_stats(source: str, destination: Path) -> None:
    if source.startswith(("http://", "https://")):
        download_http_with_retries(
            source,
            destination,
            headers={
                "User-Agent": "DomFarfora-ABC-Feed/1.0 (+GitHub Actions)",
                "Accept": "text/csv,text/plain;q=0.9,*/*;q=0.8",
            },
            description="Purchase CSV",
            timeout=180,
        )
    else:
        shutil.copyfile(source, destination)

    if destination.stat().st_size < 100:
        raise RuntimeError(
            "Downloaded purchase CSV is unexpectedly small: "
            f"{destination.stat().st_size} bytes"
        )


def custom_label_for_purchases(purchases: int) -> str:
    if purchases > 2:
        return "1"
    if purchases >= 1:
        return "2"
    return "3"


def assign_custom_labels(
    offers: ET.Element,
    purchase_counts: dict[str, int],
) -> tuple[dict[str, int], set[str]]:
    labels: Counter[str] = Counter()
    feed_name_counts: Counter[str] = Counter()
    matched_names: set[str] = set()
    seen_offer_ids: set[str] = set()
    matched_offers = 0

    for offer in offers:
        if local_name(offer.tag) != "offer":
            continue

        offer_id = normalize_text(offer.attrib.get("id"))
        if not offer_id:
            raise RuntimeError("Offer without id found")
        if offer_id in seen_offer_ids:
            raise RuntimeError(f"Duplicate offer id in source feed: {offer_id}")
        seen_offer_ids.add(offer_id)

        name_element = find_child_by_local_name(offer, "name")
        product_name = normalize_name(
            name_element.text if name_element is not None else None
        )
        if not product_name:
            raise RuntimeError(f"Offer {offer_id} has no non-empty <name>")

        feed_name_counts[product_name] += 1
        if product_name in purchase_counts:
            matched_names.add(product_name)
            matched_offers += 1

        label = custom_label_for_purchases(purchase_counts.get(product_name, 0))
        for existing in [
            child
            for child in offer
            if local_name(child.tag) == CUSTOM_LABEL_TAG
        ]:
            offer.remove(existing)

        label_element = ET.SubElement(
            offer,
            qualified_tag(offer.tag, CUSTOM_LABEL_TAG),
        )
        label_element.text = label
        labels[label] += 1

    duplicate_name_keys = {
        name for name, count in feed_name_counts.items() if count > 1
    }
    return {
        "custom_label_0_value_1": labels["1"],
        "custom_label_0_value_2": labels["2"],
        "custom_label_0_value_3": labels["3"],
        "offers_matched_by_name": matched_offers,
        "offers_unmatched_by_name": len(seen_offer_ids) - matched_offers,
        "matched_unique_name_keys": len(matched_names),
        "feed_unique_name_keys": len(feed_name_counts),
        "feed_duplicate_name_keys": len(duplicate_name_keys),
        "offers_with_ambiguous_duplicate_names": sum(
            feed_name_counts[name] for name in duplicate_name_keys
        ),
        "matched_duplicate_name_keys": len(duplicate_name_keys & matched_names),
    }, matched_names


def validate_custom_labels(
    offers: ET.Element,
    purchase_counts: dict[str, int],
) -> Counter[str]:
    labels: Counter[str] = Counter()
    seen_offer_ids: set[str] = set()

    for offer in offers:
        if local_name(offer.tag) != "offer":
            continue

        offer_id = normalize_text(offer.attrib.get("id"))
        if not offer_id:
            raise RuntimeError("Generated offer without id found")
        if offer_id in seen_offer_ids:
            raise RuntimeError(f"Duplicate generated offer id: {offer_id}")
        seen_offer_ids.add(offer_id)

        name_element = find_child_by_local_name(offer, "name")
        product_name = normalize_name(
            name_element.text if name_element is not None else None
        )
        if not product_name:
            raise RuntimeError(f"Generated offer {offer_id} has no non-empty <name>")

        label_elements = [
            child
            for child in offer
            if local_name(child.tag) == CUSTOM_LABEL_TAG
        ]
        if len(label_elements) != 1:
            raise RuntimeError(
                f"Offer {offer_id} must have exactly one <{CUSTOM_LABEL_TAG}>; "
                f"found {len(label_elements)}"
            )

        label = normalize_text(label_elements[0].text)
        if label not in ALLOWED_CUSTOM_LABELS:
            raise RuntimeError(
                f"Offer {offer_id} has invalid {CUSTOM_LABEL_TAG}: {label!r}"
            )

        expected = custom_label_for_purchases(purchase_counts.get(product_name, 0))
        if label != expected:
            raise RuntimeError(
                f"Offer {offer_id} has {CUSTOM_LABEL_TAG}={label}, expected {expected}"
            )
        labels[label] += 1

    return labels


def filter_feed(
    source_file: Path,
    blacklist_file: Path,
    purchase_counts: dict[str, int],
    purchase_file_stats: dict[str, int],
    output_file: Path,
) -> dict[str, int]:
    raw_blacklist, blocked = load_blacklist(blacklist_file)

    try:
        tree = ET.parse(source_file)
    except ET.ParseError as exc:
        raise RuntimeError(f"Source XML is invalid: {exc}") from exc

    root = tree.getroot()
    if local_name(root.tag) != "yml_catalog":
        raise RuntimeError(f"Unexpected root tag: {root.tag}")

    shop = find_child_by_local_name(root, "shop")
    if shop is None:
        raise RuntimeError("<shop> not found")
    offers = find_child_by_local_name(shop, "offers")
    if offers is None:
        raise RuntimeError("<offers> not found")

    offer_elements = [child for child in offers if local_name(child.tag) == "offer"]
    original_count = len(offer_elements)
    if original_count == 0:
        raise RuntimeError("Source feed contains zero offers")

    removed = 0
    matched_blocked: set[str] = set()
    for offer in offer_elements:
        vendor_element = find_child_by_local_name(offer, "vendor")
        vendor = vendor_element.text if vendor_element is not None else None
        normalized = normalize_vendor(vendor)
        if normalized in blocked:
            offers.remove(offer)
            removed += 1
            matched_blocked.add(normalized)

    remaining_count = original_count - removed
    if remaining_count <= 0:
        raise RuntimeError("Filtering removed every offer; refusing to publish")

    label_stats, matched_names = assign_custom_labels(offers, purchase_counts)
    validate_custom_labels(offers, purchase_counts)

    root.set(
        "date",
        datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d %H:%M"),
    )
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        mode="wb",
        suffix=".xml",
        prefix="feed-",
        dir=output_file.parent,
        delete=False,
    ) as temp:
        temp_path = Path(temp.name)

    try:
        ET.indent(tree, space="  ")
        tree.write(
            temp_path,
            encoding="utf-8",
            xml_declaration=True,
            short_empty_elements=True,
        )

        try:
            check_tree = ET.parse(temp_path)
        except ET.ParseError as exc:
            raise RuntimeError(f"Generated XML is invalid: {exc}") from exc

        check_root = check_tree.getroot()
        check_shop = find_child_by_local_name(check_root, "shop")
        check_offers = (
            find_child_by_local_name(check_shop, "offers")
            if check_shop is not None
            else None
        )
        if check_offers is None:
            raise RuntimeError("Generated XML lost <offers>")

        final_offers = [
            child for child in check_offers if local_name(child.tag) == "offer"
        ]
        if len(final_offers) != remaining_count:
            raise RuntimeError(
                f"Offer count changed after write: expected {remaining_count}, "
                f"got {len(final_offers)}"
            )

        remaining_blocked: list[str] = []
        for offer in final_offers:
            vendor_element = find_child_by_local_name(offer, "vendor")
            vendor = vendor_element.text if vendor_element is not None else None
            if normalize_vendor(vendor) in blocked:
                remaining_blocked.append(vendor or "")
        if remaining_blocked:
            raise RuntimeError(
                f"Validation failed: {len(remaining_blocked)} blocked offers remain; "
                f"examples: {remaining_blocked[:5]}"
            )

        final_label_counts = validate_custom_labels(check_offers, purchase_counts)
        for label in sorted(ALLOWED_CUSTOM_LABELS):
            expected_count = label_stats[f"custom_label_0_value_{label}"]
            if final_label_counts[label] != expected_count:
                raise RuntimeError(
                    f"Label {label} count changed after write: expected "
                    f"{expected_count}, got {final_label_counts[label]}"
                )

        if temp_path.stat().st_size < 1_000_000:
            raise RuntimeError("Generated XML is unexpectedly small")
        os.replace(temp_path, output_file)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    return {
        "blacklist": len(raw_blacklist),
        "matched_blacklist": len(matched_blocked),
        "unmatched_blacklist": len(blocked - matched_blocked),
        "offers_before": original_count,
        "offers_removed": removed,
        "offers_after": remaining_count,
        **purchase_file_stats,
        **label_stats,
        "purchase_csv_names_not_in_filtered_feed": len(
            set(purchase_counts) - matched_names
        ),
        "output_bytes": output_file.stat().st_size,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Filter the Dom Farfora YML/XML feed by vendor blacklist and assign "
            "custom_label_0 from conversions matched by product name"
        )
    )
    parser.add_argument(
        "--source",
        default=DEFAULT_SOURCE,
        help="Source feed URL or local XML path",
    )
    parser.add_argument(
        "--stats-source",
        default=DEFAULT_STATS_SOURCE,
        help="Published Google Sheets CSV URL or local CSV path",
    )
    parser.add_argument(
        "--blacklist",
        default="blocked_vendors.txt",
        help="Vendor blacklist text file",
    )
    parser.add_argument(
        "--output",
        default="feed.xml",
        help="Output XML path",
    )
    args = parser.parse_args()

    blacklist_file = Path(args.blacklist)
    output_file = Path(args.output)
    if not blacklist_file.exists():
        print(f"ERROR: blacklist file not found: {blacklist_file}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="domfarfora-abc-feed-") as tmpdir:
        source_file = Path(tmpdir) / "source.xml"
        purchase_file = Path(tmpdir) / "purchases.csv"
        try:
            download_source(args.source, source_file)
            download_purchase_stats(args.stats_source, purchase_file)
            purchase_counts, purchase_file_stats = load_purchase_stats(purchase_file)
            stats = filter_feed(
                source_file,
                blacklist_file,
                purchase_counts,
                purchase_file_stats,
                output_file,
            )
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    print("SUCCESS")
    for key, value in stats.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

