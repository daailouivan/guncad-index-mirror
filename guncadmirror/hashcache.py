import os
import pickle
import random
import time


class SdHashCache:
    def __init__(
        self, path="/data/sd_hash_cache.pkl", ttl=604800, jitter=86400, autosave=True
    ):
        self.path = path
        self.ttl = ttl
        self.jitter = jitter
        self.autosave = autosave
        self.cache = self._load_cache()

    def _now(self):
        return time.time()

    def _compute_expiry(self):
        jitter = random.uniform(-self.jitter, self.jitter)
        return self._now() + self.ttl + jitter

    def _load_cache(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "rb") as f:
                    return pickle.load(f)
            except Exception as e:
                print(f"Warning: Failed to load cache: {e}")
        return {}

    def _save_cache(self):
        try:
            with open(self.path, "wb") as f:
                pickle.dump(self.cache, f)
        except Exception as e:
            print(f"Warning: Failed to save cache: {e}")

    def is_fresh(self, sd_hash):
        expiry = self.cache.get(sd_hash)
        return expiry is not None and expiry > self._now()

    def mark_seen(self, sd_hash):
        self.cache[sd_hash] = self._compute_expiry()
        if self.autosave:
            self._save_cache()

    def should_download(self, sd_hash):
        return not self.is_fresh(sd_hash)

    def touch(self, sd_hash):
        self.mark_seen(sd_hash)

    def cleanup(self):
        """Remove expired entries (optional, not strictly needed)"""
        now = self._now()
        self.cache = {h: exp for h, exp in self.cache.items() if exp > now}
        self._save_cache()

    def save(self):
        self._save_cache()
