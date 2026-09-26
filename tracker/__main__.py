"""Run with: python -m tracker   (add --no-browser to skip opening a tab)"""

import sys
import threading
import webbrowser

import uvicorn

HOST, PORT = "127.0.0.1", 8000


def main():
    url = f"http://{HOST}:{PORT}"
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, webbrowser.open, args=[url]).start()
    print(f"Job Application Tracker running at {url}  (Ctrl+C to stop)")
    # Bound to 127.0.0.1 only: the tracker is not reachable from other devices.
    uvicorn.run("tracker.app:create_app", factory=True, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
