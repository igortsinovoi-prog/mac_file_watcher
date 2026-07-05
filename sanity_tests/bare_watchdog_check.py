"""Sanity check: does bare watchdog/FSEvents see a write to a file in /tmp?

Not part of the pytest suite -- a standalone diagnostic to isolate whether
a "no events" symptom is in our code or in the underlying watchdog/FSEvents
stack on this machine.
"""
import os
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

TARGET = "/tmp/wd_target.txt"


class PrintingHandler(FileSystemEventHandler):
    def on_any_event(self, event):
        print("EVENT", event)


def main():
    observer = None
    try:
        with open(TARGET, "w"):
            pass

        observer = Observer()
        observer.schedule(PrintingHandler(), "/tmp", recursive=False)
        observer.start()
        time.sleep(1)
        with open(TARGET, "a") as handle:
            handle.write("hello\n")
        time.sleep(2)
        print("done")
    finally:
        if observer is not None:
            observer.stop()
            observer.join()
        if os.path.exists(TARGET):
            os.remove(TARGET)


if __name__ == "__main__":
    main()
