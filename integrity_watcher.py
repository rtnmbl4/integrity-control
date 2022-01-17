from sys import platform

from watchdog.events import FileClosedEvent, PatternMatchingEventHandler
from watchdog.observers import Observer

import integrity_lib as ilib


class DatabaseEventHandler(PatternMatchingEventHandler):
    def on_any_event(self, event):
        if not isinstance(event, FileClosedEvent):
            with ilib.connect_to_auxiliary_db() as connection:
                try:
                    pk = ilib.select_file_id(connection, event.src_path)
                    ilib.mark_as_incorrect(connection, "files", pk)
                    ilib.insert_into_aux_table(
                        connection,
                        "file_errors",
                        ["file_id", "checked_at", "manual"],
                        [str(pk), str(ilib.get_current_timestamp()), "0"],
                    )
                    connection.commit()
                except ilib.IntegrityLibError:
                    pass


def main():
    with ilib.connect_to_auxiliary_db() as connection:
        file_paths = ilib.select_watched_files(connection)
    observer = Observer()
    event_handler = DatabaseEventHandler(file_paths, ignore_directories=True)
    observer.schedule(event_handler, "E:\\", recursive=True)
    observer.start()
    try:
        while observer.is_alive():
            observer.join(1)
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
