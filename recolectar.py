# Recolector de subastas de WoW Forever para GitHub Actions (wowecon).
# Uso: python recolectar.py <carpeta_datos> <carpeta_estado>
import base64, email.utils, gzip, json, os, sys, time
import urllib.error, urllib.parse, urllib.request
from collections import defaultdict
from pathlib import Path

MAY_EXPIRE = {"SHORT", "MEDIUM"}
MAX_GAP = 3 * 3600
FRACTION, ISO_RATIO, ISO_SHARE = 0.15, 0.5, 0.02


def localized(value):
    if isinstance(value, dict):
        return str(value.get("en_US") or next(iter(value.values()), "") or "")
    return str(value or "")


class Blizzard:
    def __init__(self, client_id, client_secret, region):
        self.id, self.secret, self.region, self.token = client_id, client_secret, region, None

    def auth(self):
        if self.token is None:
            cred = base64.b64encode((self.id + ":" + self.secret).encode()).decode()
            req = urllib.request.Request(
                "https://oauth.battle.net/token",
                data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
                headers={"Authorization": "Basic " + cred,
                         "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=120) as r:
                self.token = json.loads(r.read())["access_token"]
        return self.token

    def get(self, path, since=None, retry=True):
        headers = {"Authorization": "Bearer " + self.auth(),
                   "Battlenet-Namespace": "dynamic-forever-" + self.region}
        if since:
            headers["If-Modified-Since"] = since
        url = "https://" + self.region + ".api.blizzard.com" + path
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=180) as r:
                return json.load(r), r.headers.get("Last-Modified")
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return None, since
            if e.code == 404:
                return {}, since
            if e.code == 401 and retry:
                self.token = None
                return self.get(path, since, False)
            raise

    def houses(self):
        data, _ = self.get("/data/wow/ruleset/1/auctions/")
        return [(h["id"], localized(h.get("name")) or "Casa %d" % h["id"]) for h in data.get("auctions", [])]

    def fetch_house(self, house_id, since=None):
        base = "/data/wow/ruleset/1/auctions/%d" % house_id
        data, modified = self.get(base, since)
        if data is None:
            return None
        commodities, _ = self.get(base + "/commodities")
        return data.get("auctions") or [], (commodities or {}).get("auctions") or [], modified


def entries(raw):
    for e in raw:
        qty = e.get("quantity") or 0
        if qty <= 0:
            continue
        if "unit_price" in e:
            price = e["unit_price"]
        elif e.get("buyout"):
            price = e["buyout"] // qty
        else:
            continue
        if price > 0:
            yield e["id"], e["item"]["id"], price, qty, e.get("time_left")


def market_price(book):
    take = max(1, int(sum(q for _, q in book) * FRACTION))
    spent, left = 0, take
    for price, qty in book:
        n = min(qty, left)
        spent += price * n
        left -= n
        if left == 0:
            break
    return round(spent / take)


def summarize(book):
    book = sorted(book)
    total = sum(q for _, q in book)
    isolated = 0
    while len(book) > 1:
        price, qty = book[0]
        if price < ISO_RATIO * market_price(book[1:]) and isolated + qty <= ISO_SHARE * total:
            isolated += qty
            book = book[1:]
        else:
            break
    return [book[0][0], market_price(book), sum(q for _, q in book), len(book), isolated]


def stamp_of(modified):
    try:
        return int(email.utils.parsedate_to_datetime(modified).timestamp())
    except (TypeError, ValueError):
        return int(time.time())


def collect_house(client, region, house_id, name, data_dir, state_dir):
    state_file = state_dir / ("%s-%d.json.gz" % (region, house_id))
    previous = None
    if state_file.exists():
        with gzip.open(state_file, "rt", encoding="utf-8") as f:
            previous = json.load(f)
    got = client.fetch_house(house_id, previous.get("last_modified") if previous else None)
    if got is None:
        return "sin cambios"
    auctions, commodities, modified = got
    stamp = stamp_of(modified)
    out = data_dir / "snapshots" / region / str(house_id) / ("%d.json.gz" % stamp)
    if out.exists():
        return "repetido"
    book, per_item = {}, defaultdict(list)
    for auction_id, item, price, qty, left in entries(list(auctions) + list(commodities)):
        book[str(auction_id)] = [item, qty, left]
        per_item[item].append((price, qty))
    gone = defaultdict(int)
    if previous and stamp - previous.get("stamp", 0) <= MAX_GAP:
        for auction_id, (item, qty, left) in previous["book"].items():
            if left in MAY_EXPIRE:
                continue
            now = book.get(auction_id)
            lost = qty - (now[1] if now else 0)
            if lost > 0:
                gone[item] += lost
    rows = [[item] + summarize(listing) + [gone.get(item, 0)] for item, listing in sorted(per_item.items())]
    rows += [[item, 0, 0, 0, 0, 0, units] for item, units in gone.items() if item not in per_item]
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", encoding="utf-8") as f:
        json.dump({"format": 1, "region": region, "house_id": house_id, "name": name,
                   "fetched_at": stamp, "listings": len(book), "rows": rows}, f, separators=(",", ":"))
    state_dir.mkdir(parents=True, exist_ok=True)
    with gzip.open(state_file, "wt", encoding="utf-8") as f:
        json.dump({"last_modified": modified, "stamp": stamp, "book": book}, f, separators=(",", ":"))
    return "nuevo"


def prune(data_dir, keep_days, now=None):
    limit = (now or int(time.time())) - keep_days * 86400
    removed = 0
    for path in (data_dir / "snapshots").glob("*/*/*.json.gz"):
        if int(path.name.split(".")[0]) < limit:
            path.unlink()
            removed += 1
    return removed


def main(argv):
    data_dir, state_dir = Path(argv[1]), Path(argv[2])
    region = os.environ.get("WOWECON_REGION", "eu").strip().lower()
    wanted = {int(x) for x in os.environ.get("WOWECON_HOUSES", "").replace(",", " ").split()}
    client = Blizzard(os.environ["BNET_CLIENT_ID"], os.environ["BNET_CLIENT_SECRET"], region)
    houses = [h for h in client.houses() if not wanted or h[0] in wanted]
    failures = 0
    for house_id, name in houses:
        try:
            print("casa", house_id, name, "->", collect_house(client, region, house_id, name, data_dir, state_dir))
        except Exception as e:
            failures += 1
            print("casa", house_id, "ERROR", repr(e))
    data_dir.mkdir(parents=True, exist_ok=True)
    print("snapshots antiguos borrados:", prune(data_dir, int(os.environ.get("WOWECON_KEEP_DAYS", 21))))
    (data_dir / "LEEME.txt").write_text("Datos recogidos cada hora por wowecon. No editar a mano.", encoding="utf-8")
    return 1 if houses and failures == len(houses) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
