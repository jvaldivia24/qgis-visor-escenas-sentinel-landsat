import requests


class StacClient:
    def __init__(self, base_url: str, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def search(self, collections, bbox, datetime_range, limit=30, query=None):
        url = f"{self.base_url}/search"
        payload = {
            "collections": collections,
            "bbox": bbox,
            "datetime": datetime_range,
            "limit": int(limit),
        }
        if query:
            payload["query"] = query
        r = requests.post(url, json=payload, timeout=self.timeout)
        r.raise_for_status()
        return r.json()
