import json
import sys
from datetime import datetime
from os.path import getsize
from typing import Optional

from tabulate import tabulate

import integrity_lib as ilib

COMMANDS = (
    "help",
    "check",
    "full_check",
    "db_connect",
    "add",
    "exit",
    "list_incorrect",
    "list_algorithms",
    "watch",
    "remove",
    "restore",
)


OBJECTS = ("file", "table")
OBJECTS_PLURAL = ("files", "tables")
DBMS = ("mysql", "postgresql")


class REPL:
    def __init__(self):
        self.aux_connection = ilib.connect_to_auxiliary_db()
        self.aux_connection.execute("PRAGMA foreign_keys = ON;")
        self.error = False  # Скрипты будут выполняться до первой ошибки
        self.connection = None
        self.backup_dir = None
        self.last_check_no_error = None

    def _get_database_id(self):
        return ilib.get_database_id(
            self.aux_connection,
            ilib.get_connection_name(
                ilib.make_connection_string(*self.connection.engine.url[0:6])
            ),
        )

    @staticmethod
    def help() -> str:
        strings = [
            "команда [аргумент1 аргумент2...]",
            "Синтаксис справки:",
            "- команда: Описание.",
            "├- обязательный аргумент (допустимое значение/допустимое значение)",
            "└-[опциональный аргумент]",
            "-" * 30,
        ]
        with open("commands.json", encoding="utf-8") as commands:
            raw_data = json.load(commands)
        for command, props in raw_data.items():
            strings.append(f"- {command}: {props['description']}")
            if "args" in props:
                for i, arg_dict in enumerate(props["args"]):
                    arg_str = (
                        ("├-" if i < len(props["args"]) - 1 else "└-")
                        + (" " if arg_dict["required"] else "[")
                        + arg_dict["description"]
                        + ("" if arg_dict["required"] else "]")
                    )
                    if "possible_values" in arg_dict:
                        possible_values = "/".join(arg_dict["possible_values"])
                        arg_str += f" ({possible_values})"
                    strings.append(arg_str)
        return "\n".join(strings)

    def _add_file(
        self,
        path: str,
        algorithm_id: int,
        algorithm_name: str,
        watch: bool,
        backup: bool,
    ) -> str:
        try:
            file = open(path, "rb")
        except FileNotFoundError:
            self.error = True
            return f'Файл "{path}" не найден'
        digest = ilib.calculate_checksum(file.read(), algorithm_name)
        if not digest:
            raise ilib.ParamError(
                f'Не удалось рассчитать контрольную сумму файла "{path}"'
            )
        insert_params = {
            "path": path,
            "file_size": str(getsize(path)),
            "is_watched": str(int(watch)),
            "algorithm_id": str(algorithm_id),
            "checksum": digest,
            "is_correct": "1",
            "calculated_at": str(ilib.get_current_timestamp()),
        }
        ilib.insert_into_aux_table(
            self.aux_connection,
            "files",
            list(insert_params.keys()),
            list(insert_params.values()),
        )
        self.aux_connection.commit()
        message = f"Файл {path} добавлен"
        if backup:
            file.seek(0)
            if ilib.make_compressed_copy(file.read(), "file", digest, self.backup_dir):
                message += "\nСоздана сжатая резервная копия"
        return message

    def _add_table(
        self,
        name: str,
        pk_field: Optional[str],
        algorithm_id: int,
        algorithm_name: str,
        backup: bool,
    ) -> str:
        count = ilib.select_count(self.connection, name)
        full_data = ilib.select_all_from_table(self.connection, name, pk_field or "id")
        digest = ilib.calculate_checksum(
            bytes(full_data.encode(self.connection.connection.encoding)),
            algorithm_name,
        )
        database_id = self._get_database_id()
        if not digest:
            raise ilib.ParamError(
                f'Не удалось рассчитать контрольную сумму таблицы "{name}"'
            )
        insert_params = {
            "table_name": name,
            "checksum": digest,
            "database_id": str(database_id),
            "algorithm_id": str(algorithm_id),
            "row_count": str(count),
            "is_correct": "1",
            "calculated_at": str(ilib.get_current_timestamp()),
            "pk_field": pk_field,
        }
        ilib.insert_into_aux_table(
            self.aux_connection,
            "tables",
            list(insert_params.keys()),
            list(insert_params.values()),
        )
        self.aux_connection.commit()
        message = f"Таблица {name} добавлена"
        if not pk_field:
            message += '\nПРЕДУПРЕЖДЕНИЕ: в качестве первичного ключа было автоматически выбрано поле "id"'
        return message

    def add(
        self,
        algorithm: str = None,
        what: str = None,
        path_or_name: str = None,
        *opt_args,
    ) -> str:
        if not all([what, algorithm, path_or_name]):
            self.error = True
            return 'Недостаточно параметров для команды "add"'
        if what not in OBJECTS:
            self.error = True
            return f'"{what}" не является правильным аргументом для команды "add"'
        try:
            algo_id = ilib.get_algorithm_id(self.aux_connection, algorithm)
        except ilib.ParamError as e:
            self.error = True
            return e.message
        backup = "backup" in opt_args
        try:
            if what == "file":
                watch = "watch" in opt_args
                return self._add_file(path_or_name, algo_id, algorithm, watch, backup)
            if what == "table":
                if not self.connection:
                    return "Невозможно добавить таблицу без соединения с базой данных"
                pk_field = opt_args[0] if opt_args else None
                return self._add_table(
                    path_or_name, pk_field, algo_id, algorithm, backup
                )
        except (ilib.ParamError, ilib.ParamTypeError) as e:
            self.error = True
            return e.message

    def db_connect(
        self,
        host: str = None,
        port: str = None,
        database: str = None,
        login: str = None,
        password: str = None,
        dbms: Optional[str] = None,
    ) -> str:
        if not all([host, port, database, login, password]):
            self.error = True
            return 'Недостаточно параметров для команды "db_connect"'
        if dbms is None:
            if port == "3306":
                dbms = "mysql"
            elif port == "5432":
                dbms = "postgresql"
            else:
                self.error = True
                return "Не удалось определить СУБД, соединение не установлено"
        if dbms not in DBMS:
            self.error = True
            return f'"{dbms}" не является допустимой СУБД'
        try:
            self.connection = ilib.connect_to_db(
                ilib.make_connection_string(
                    dbms, login, password, host, port, database
                ),
                self.aux_connection,
            )
            self.aux_connection.commit()
            return f"Соединение с базой {database} установлено"
        except ilib.IntegrityLibError as e:
            self.error = True
            return e.message

    def list_algorithms(self) -> str:
        algo_list = ilib.select_algorithms(self.aux_connection)
        algo_formatted_list = "\n".join([f"- {algo}" for algo in algo_list])
        return f"Доступны следующие алгоритмы:\n{algo_formatted_list}"

    def _check_file(self, path: str) -> str:
        pk, checksum, algorithm_name = ilib.get_reference_checksum(
            self.aux_connection, "files", ("id", "checksum"), {"path": path}
        )
        try:
            file = open(path, "rb")
        except FileNotFoundError:
            self.error = True
            return f'Файл "{path}" не найден'
        digest = ilib.calculate_checksum(file.read(), algorithm_name)
        self.last_check_no_error = int(digest, base=16) == int(checksum, base=16)
        if self.last_check_no_error:
            return f'Целостность файла "{path}" соблюдена'
        else:
            ilib.mark_as_incorrect(self.aux_connection, "files", pk)
            ilib.insert_into_aux_table(
                self.aux_connection,
                "file_errors",
                ["file_id", "checked_at", "manual"],
                [str(pk), str(ilib.get_current_timestamp()), "1"],
            )
            self.aux_connection.commit()
            return f'Целостность файла "{path}" нарушена!'

    def _check_table(self, name: str) -> str:
        pk, checksum, pk_field, algorithm_name = ilib.get_reference_checksum(
            self.aux_connection,
            "tables",
            ("id", "checksum", "pk_field"),
            {
                "table_name": name,
                "database_id": self._get_database_id(),
            },
        )
        full_data = ilib.select_all_from_table(self.connection, name, pk_field or "id")
        digest = ilib.calculate_checksum(
            bytes(full_data.encode(self.connection.connection.encoding)),
            algorithm_name,
        )
        self.last_check_no_error = int(digest, base=16) == int(checksum, base=16)
        if self.last_check_no_error:
            return f'Целостность таблицы "{name}" соблюдена'
        else:
            ilib.mark_as_incorrect(self.aux_connection, "tables", pk)
            ilib.insert_into_aux_table(
                self.aux_connection,
                "table_errors",
                ["table_id", "checked_at"],
                [str(pk), str(ilib.get_current_timestamp())],
            )
            self.aux_connection.commit()
            return f'Целостность таблицы "{name}" нарушена!'

    def check(self, what: str = None, path_or_name: str = None) -> str:
        if not all([what, path_or_name]):
            self.error = True
            return 'Недостаточно параметров для команды "check"'
        if what not in OBJECTS:
            self.error = True
            return f'"{what}" не является правильным аргументом для команды "check"'
        try:
            if what == "file":
                return self._check_file(path_or_name)
            if what == "table":
                if not self.connection:
                    self.error = True
                    return "Невозможно проверить таблицу без соединения с базой данных"
                return self._check_table(path_or_name)
        except ilib.IntegrityLibError as e:
            self.error = True
            return e.message

    def remove(self, what: str = None, path_or_name: str = None) -> str:
        if not all([what, path_or_name]):
            self.error = True
            return 'Недостаточно параметров для команды "remove"'
        if what not in OBJECTS:
            self.error = True
            return f'"{what}" не является правильным аргументом для команды "remove"'
        try:
            if what == "file":
                ilib.delete_from_aux_table(
                    self.aux_connection, "files", {"path": path_or_name}
                )
                self.aux_connection.commit()
                return f"Запись о файле {path_or_name} удалена"
            if what == "table":
                if not self.connection:
                    self.error = True
                    return "Невозможно удалить таблицу без соединения с базой данных"

                ilib.delete_from_aux_table(
                    self.aux_connection,
                    "tables",
                    {
                        "table_name": path_or_name,
                        "database_id": self._get_database_id(),
                    },
                )
                self.aux_connection.commit()
                return f"Запись о таблице {path_or_name} удалена"
        except ilib.IntegrityLibError as e:
            self.error = True
            return e.message

    def list_incorrect(self, what: str = None) -> str:
        if not what:
            self.error = True
            return 'Недостаточно параметров для команды "list_incorrect"'
        if what not in OBJECTS_PLURAL:
            self.error = True
            return f'"{what}" не является правильным аргументом для команды "list_incorrect"'
        try:
            if what == "files":
                results = ilib.select_incorrect(
                    self.aux_connection,
                    "files",
                    "path",
                    "file_id",
                )
                header = "Файл"
            if what == "tables":
                results = ilib.select_incorrect(
                    self.aux_connection,
                    "tables",
                    "table_name",
                    "table_id",
                )
                header = "Таблица"
            results = [
                (name, datetime.fromtimestamp(checked_at))
                for name, checked_at in results
                if checked_at is not None
            ]
            return tabulate(results, (header, "Дата проверки"), "github")
        except ilib.IntegrityLibError as e:
            self.error = True
            return e.message

    def restore(self, what: str = None, path_or_name: str = None):
        if not all([what, path_or_name]):
            self.error = True
            return 'Недостаточно параметров для команды "restore"'
        if what not in OBJECTS:
            self.error = True
            return f'"{what}" не является правильным аргументом для команды "restore"'
        if what == "file":
            pk, checksum, algorithm_name = ilib.get_reference_checksum(
                self.aux_connection, "files", ("id", "checksum"), {"path": path_or_name}
            )
        if what == "table":
            pk, checksum, pk_field, algorithm_name = ilib.get_reference_checksum(
                self.aux_connection,
                "tables",
                ("id", "checksum", "pk_field"),
                {
                    "table_name": path_or_name,
                    "database_id": self._get_database_id(),
                },
            )
        try:
            ilib.restore_backup(what, path_or_name, algorithm_name, checksum)
            return "Восстановление завершено"
        except ilib.IntegrityLibError as e:
            return e.message


def parse_quotes(raw_list):
    args = []
    quoted = ""
    in_quotes = False
    for arg in raw_list:
        if arg[0] == '"' and not in_quotes:
            if arg[-1] == '"':
                args.append(arg[1:-1])
                continue
            quoted = arg[1:]
            in_quotes = True
            continue
        if in_quotes:
            if arg[-1] == '"':
                quoted += f" {arg[:-1]}"
                args.append(quoted)
                in_quotes = False
                quoted = ""
                continue
            quoted += f" {arg}"
            continue
        args.append(arg)
    return args


if __name__ == "__main__":
    current_line = 0
    operator = None
    args = []
    if len(sys.argv) > 1:
        operator = sys.argv[1]
        args = sys.argv[2:]
    repl = REPL()
    while True:
        current_line += 1
        user_input = input(f"COM[{current_line}]: ")
        command = user_input.split()
        if not command:
            continue
        operator = command[0]
        if operator == "exit":
            break
        args = parse_quotes(command[1:])
        if operator in COMMANDS:
            print(getattr(repl, operator)(*args))
        else:
            print(
                f'Неправильная команда "{operator}"; для получения списка команд введите "help"'
            )
    print("Выход")
