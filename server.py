import ast
import csv
import json
import re
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen


# HOST = "127.0.0.1"
HOST = "0.0.0.0"
# PORT = 8000
PORT = int(os.environ.get("PORT", 10000)) # Default to 10000 if PORT isn't set
DATA_URL = "https://gaswatchph.com/js/data.js"
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CACHE_JSON = DATA_DIR / "latest-prices.json"
CACHE_CSV = DATA_DIR / "latest-prices.csv"
CACHE_TTL_SECONDS = 300


def slugify(value):
    text = re.sub(r"\s+", "-", (value or "").strip().lower())
    text = re.sub(r"[^\w-]", "", text)
    text = re.sub(r"-{2,}", "-", text)
    return text or "unknown"


class GasWatchService:
    def __init__(self):
        self.data_url = DATA_URL
        self.refresh_lock = Lock()

    def fetch_data_script(self):
        request = Request(
            self.data_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                )
            },
        )
        with urlopen(request, timeout=20) as response:
            return response.read().decode("utf-8")

    def extract_js_value(self, script_text, variable_name):
        marker = f"const {variable_name} ="
        start = script_text.find(marker)
        if start == -1:
            raise ValueError(f"Could not find '{variable_name}' in GasWatch data.")

        index = start + len(marker)
        while index < len(script_text) and script_text[index].isspace():
            index += 1

        opening = script_text[index]
        if opening == "[":
            closing = "]"
        elif opening == "{":
            closing = "}"
        elif opening == '"':
            end = script_text.find('"', index + 1)
            while end != -1 and script_text[end - 1] == "\\":
                end = script_text.find('"', end + 1)
            if end == -1:
                raise ValueError(f"Could not parse string value for '{variable_name}'.")
            return script_text[index : end + 1]
        else:
            end = script_text.find(";", index)
            return script_text[index:end].strip()

        depth = 0
        in_string = False
        string_char = ""
        escaped = False

        for end in range(index, len(script_text)):
            char = script_text[end]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == string_char:
                    in_string = False
                continue

            if char in ('"', "'"):
                in_string = True
                string_char = char
                continue

            if char == opening:
                depth += 1
            elif char == closing:
                depth -= 1
                if depth == 0:
                    return script_text[index : end + 1]

        raise ValueError(f"Could not parse '{variable_name}' from GasWatch data.")

    def strip_js_comments(self, value):
        result = []
        in_string = False
        string_char = ""
        escaped = False
        index = 0

        while index < len(value):
            char = value[index]
            next_char = value[index + 1] if index + 1 < len(value) else ""

            if in_string:
                result.append(char)
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == string_char:
                    in_string = False
                index += 1
                continue

            if char in ('"', "'"):
                in_string = True
                string_char = char
                result.append(char)
                index += 1
                continue

            if char == "/" and next_char == "/":
                index += 2
                while index < len(value) and value[index] not in "\r\n":
                    index += 1
                continue

            result.append(char)
            index += 1

        return "".join(result)

    def to_python_literal(self, js_value):
        js_value = self.strip_js_comments(js_value)
        js_value = re.sub(r"(\{|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", r'\1 "\2":', js_value)
        js_value = js_value.replace("null", "None")
        js_value = js_value.replace("true", "True").replace("false", "False")
        return ast.literal_eval(js_value)

    def load_payload(self):
        script_text = self.fetch_data_script()
        return {
            "stations": self.to_python_literal(self.extract_js_value(script_text, "GAS_STATIONS")),
            "brands": self.to_python_literal(self.extract_js_value(script_text, "BRANDS")),
            "fuel_types": self.to_python_literal(self.extract_js_value(script_text, "FUEL_TYPES")),
            "last_updated": ast.literal_eval(self.extract_js_value(script_text, "LAST_UPDATED")),
        }

    def build_cache(self):
        payload = self.load_payload()
        location_map = {}
        grouped = {}
        csv_rows = []

        for station in payload["stations"]:
            brand_key = station.get("brand", "")
            brand_name = payload["brands"].get(brand_key, {}).get("name", brand_key.title())
            location_name = station.get("area") or "Unknown"
            location_slug = slugify(location_name)
            station_name = station.get("name") or "Unnamed Station"
            station_slug = slugify(f"{brand_name}-{station_name}")
            station_record = grouped.setdefault(location_slug, {}).setdefault(
                station_slug,
                {
                    "id": station_slug,
                    "brand": brand_name,
                    "station": station_name,
                    "prices": {},
                    "last_updated": payload["last_updated"],
                },
            )
            location_map[location_slug] = location_name

            for fuel_key, fuel_label in payload["fuel_types"].items():
                price = station.get("prices", {}).get(fuel_key)
                if price is None:
                    continue

                station_record["prices"][fuel_key] = price
                csv_rows.append(
                    {
                        "location": location_name,
                        "brand": brand_name,
                        "station": station_name,
                        "fuel_type": fuel_label,
                        "fuel_key": fuel_key,
                        "price": price,
                        "last_updated": payload["last_updated"],
                        "source": self.data_url,
                    }
                )

        locations = [
            {"slug": slug, "name": name}
            for slug, name in sorted(location_map.items(), key=lambda item: item[1].lower())
        ]
        city_data = {
            slug: sorted(
                grouped[slug].values(),
                key=lambda station: (station["brand"].lower(), station["station"].lower()),
            )
            for slug in grouped
        }
        result = {
            "source": self.data_url,
            "last_updated": payload["last_updated"],
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "locations": locations,
            "city_data": city_data,
            "total_locations": len(locations),
            "total_stations": sum(len(stations) for stations in city_data.values()),
            "total_prices": len(csv_rows),
        }
        return result, csv_rows

    def write_cache(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        cache_payload, csv_rows = self.build_cache()
        CACHE_JSON.write_text(json.dumps(cache_payload, indent=2), encoding="utf-8")
        with CACHE_CSV.open("w", newline="", encoding="utf-8") as file_handle:
            writer = csv.DictWriter(
                file_handle,
                fieldnames=[
                    "location",
                    "brand",
                    "station",
                    "fuel_type",
                    "fuel_key",
                    "price",
                    "last_updated",
                    "source",
                ],
            )
            writer.writeheader()
            writer.writerows(csv_rows)
        return cache_payload

    def read_cache(self):
        if not CACHE_JSON.exists():
            return None
        return json.loads(CACHE_JSON.read_text(encoding="utf-8"))

    def get_cache_age_seconds(self, cache_payload):
        if not cache_payload:
            return None

        synced_at = cache_payload.get("synced_at")
        if not synced_at:
            return None

        normalized = synced_at.replace("Z", "+00:00")
        synced_at_dt = datetime.fromisoformat(normalized)
        return max(0, int((datetime.now(timezone.utc) - synced_at_dt).total_seconds()))

    def sync_cache(self, force=False):
        cache_payload = self.read_cache()
        cache_age_seconds = self.get_cache_age_seconds(cache_payload)
        is_fresh = (
            cache_payload is not None
            and cache_age_seconds is not None
            and cache_age_seconds < CACHE_TTL_SECONDS
        )

        if is_fresh and not force:
            return cache_payload, False, cache_age_seconds

        with self.refresh_lock:
            cache_payload = self.read_cache()
            cache_age_seconds = self.get_cache_age_seconds(cache_payload)
            is_fresh = (
                cache_payload is not None
                and cache_age_seconds is not None
                and cache_age_seconds < CACHE_TTL_SECONDS
            )
            if is_fresh and not force:
                return cache_payload, False, cache_age_seconds

            cache_payload = self.write_cache()
            cache_age_seconds = self.get_cache_age_seconds(cache_payload)
            return cache_payload, True, cache_age_seconds


service = GasWatchService()


class GasTrackHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def end_json(self, status, payload):
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/prices":
            cache_payload = service.read_cache()
            if cache_payload is None:
                self.end_json(
                    HTTPStatus.NOT_FOUND,
                    {
                        "status": "missing",
                        "message": "No local cache found yet. Click Update Live Prices first.",
                    },
                )
                return

            self.end_json(
                HTTPStatus.OK,
                {
                    "status": "success",
                    "data": cache_payload,
                    "meta": {
                        "cache_age_seconds": service.get_cache_age_seconds(cache_payload),
                        "cache_ttl_seconds": CACHE_TTL_SECONDS,
                    },
                },
            )
            return

        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/update":
            self.end_json(HTTPStatus.NOT_FOUND, {"status": "error", "message": "Not found."})
            return

        try:
            params = parse_qs(parsed.query)
            force = params.get("force", ["0"])[0] == "1"
            cache_payload, refreshed, cache_age_seconds = service.sync_cache(force=force)
            self.end_json(
                HTTPStatus.OK,
                {
                    "status": "success",
                    "message": (
                        "Fuel prices updated successfully."
                        if refreshed
                        else "Using recent cached prices."
                    ),
                    "data": cache_payload,
                    "meta": {
                        "refreshed": refreshed,
                        "cache_age_seconds": cache_age_seconds,
                        "cache_ttl_seconds": CACHE_TTL_SECONDS,
                    },
                },
            )
        except HTTPError as error:
            self.end_json(
                HTTPStatus.BAD_GATEWAY,
                {"status": "error", "message": f"GasWatch returned HTTP {error.code}."},
            )
        except URLError as error:
            self.end_json(
                HTTPStatus.BAD_GATEWAY,
                {"status": "error", "message": f"Network error: {error.reason}"},
            )
        except Exception as error:
            self.end_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"status": "error", "message": str(error)},
            )


def main():
    server = ThreadingHTTPServer((HOST, PORT), GasTrackHandler)
    print(f"GasTrack PH server running at http://{HOST}:{PORT}")
    print("Use the Update Live Prices button in the browser to refresh data.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
