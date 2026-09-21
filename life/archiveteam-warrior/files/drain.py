"""Stop claiming items, then keep preStop open while Warrior finishes them.

The kubelet owns the deadline via terminationGracePeriodSeconds. Sending the
image stop signal to its Python launcher can exit before the worker drains.
"""
import base64
import json
import socket
import time
import urllib.error
import urllib.request


def listening():
    try:
        with socket.create_connection(("127.0.0.1", 8001), timeout=2):
            return True
    except OSError:
        return False


def main():
    # Honor credentials if someone set them through the private admin UI.
    try:
        with open("/home/warrior/projects/config.json") as config_file:
            config = json.load(config_file)
    except (OSError, ValueError):
        config = {}
    headers = {}
    if config.get("http_password"):
        credentials = "{}:{}".format(
            config.get("http_username") or "", config["http_password"]
        )
        headers["Authorization"] = "Basic " + base64.b64encode(
            credentials.encode()
        ).decode()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while listening():
        try:
            request = urllib.request.Request(
                "http://127.0.0.1:8001/api/stop", data=b"", headers=headers
            )
            with opener.open(request, timeout=5) as response:
                if response.read().strip() != b"OK":
                    raise RuntimeError("Warrior did not acknowledge stop")
            break
        except (OSError, urllib.error.URLError, RuntimeError) as error:
            print("Waiting for Warrior stop endpoint: {}".format(error), flush=True)
            time.sleep(1)
    while listening():
        time.sleep(1)


if __name__ == "__main__":
    main()
