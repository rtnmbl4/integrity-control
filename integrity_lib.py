import hashlib
import os
import sqlite3
import zlib
from datetime import datetime
from pathlib import Path
from os.path import exists
from typing import List, Tuple, Dict, Optional

from crc64iso.crc64iso import format_crc64_pair, crc64_pair
from pygost import gost341194, gost34112012256, gost34112012512
from pygost.utils import hexenc
from sqlalchemy import create_engine
from sqlalchemy.engine import Connectable
from sqlalchemy.exc import OperationalError, ProgrammingError


class IntegrityLibError(Exception):
    """Базовый класс исключений. Не использовать сам по себе."""

    message = "ОШИБКА БИБЛИОТЕКИ ОБЕСПЕЧЕНИЯ ЦЕЛОСТНОСТИ"


class ParamTypeError(IntegrityLibError):
    def __init__(self, message):
        self.message = f"НЕПРАВИЛЬНЫЙ ТИП ПАРАМЕТРА: {message}"


class ParamError(IntegrityLibError):
    def __init__(self, message):
        self.message = f"ПЕРЕДАН НЕПРАВИЛЬНЫЙ ПАРАМЕТР: {message}"


class DatabaseError(IntegrityLibError):
    def __init__(self, message):
        self.message = f"ОШИБКА ВСПОМОГАТЕЛЬНОЙ БАЗЫ ДАННЫХ: {message}"


def get_current_timestamp() -> int:
    """
    Текущие дата и время в формате UTC.
    :return:
    """
    return int(datetime.utcnow().timestamp())


def make_connection_string(dbms, login, password, host, port, database):
    if dbms == "mysql":
        dbms += "+pymysql"
    return f"{dbms}://{login}:{password}@{host}:{port}/{database}"


def get_connection_name(connection_string):
    return hashlib.md5(bytes(connection_string.encode("utf-8"))).hexdigest()


# Общий функционал


def calculate_checksum(obj: bytes, algorithm: str = "crc32") -> str:
    """
    Рассчитывает контрольную сумму объекта и возвращает строку-hexdigest.
    :param obj:
    :param algorithm:
    :return:
    """
    if not isinstance(obj, bytes):
        raise ParamTypeError(
            "Расчёт контрольной суммы возможен только для последовательности байтов"
        )
    if algorithm in ("crc32", "adler32"):
        return hex(getattr(zlib, algorithm)(obj))[2:]
    if algorithm in hashlib.algorithms_guaranteed:
        try:
            return getattr(hashlib, algorithm)(obj).hexdigest()
        except TypeError:
            return getattr(hashlib, algorithm)(obj).hexdigest(256)
    if algorithm == "crc64":
        return format_crc64_pair(crc64_pair(obj)).lower()
    if algorithm in ("gost94", "gost_256", "gost_512"):
        return hexenc(
            {
                "gost94": gost341194,
                "gost_256": gost34112012256,
                "gost_512": gost34112012512,
            }[algorithm]
            .new(obj)
            .digest()
        )
    raise ParamError("Указан неправильный алгоритм")


def make_compressed_copy(
    obj: bytes, obj_type: str, checksum: str, backup_dir: Optional[str]
) -> bool:
    """
    Сохраняет сжатую копию защищаемого объекта
    :param obj:
    :param obj_type:
    :param checksum:
    :param backup_dir:
    :return:
    """
    try:
        compressed_data = zlib.compress(obj, 9)
    except zlib.error:
        return False
    backup_path = backup_dir if backup_dir is not None else os.getcwd() + "/backups"
    backup_path += "/" + obj_type + "s"
    Path(backup_path).mkdir(parents=True, exist_ok=True)
    with open(backup_path + "/" + checksum, "wb") as file:
        file.write(compressed_data)
    return True


def restore_backup(obj_type: str, path_or_name: str, algorithm: str, checksum: str):
    if obj_type == "table":
        raise ParamError("Восстановление резервной копии таблицы не реализовано")
    try:
        with open(f"backups/{obj_type}s/{checksum}", "rb") as backup:
            backup_data = zlib.decompress(backup.read())
    except FileNotFoundError:
        raise ParamError("Резервная копия не найдена")
    backup_checksum = calculate_checksum(backup_data, algorithm)
    if int(backup_checksum, base=16) != int(checksum, base=16):
        raise ParamError("Резервная копия повреждена, восстановление невозможно")
    with open(path_or_name, "wb") as file:
        file.write(backup_data)


# Работа с вспомогательной БД


def connect_to_auxiliary_db() -> sqlite3.Connection:
    """
    Обеспечивает соединение со вспомогательной базой данных.
    Если её ещё нет, создаёт её.
    :return: объект соединения
    """
    if not exists("integrity_db.db"):
        with open("db_init.sql") as init_script, sqlite3.connect(
            "integrity_db.db"
        ) as init_con:
            cur = init_con.cursor()
            cur.executescript(init_script.read())
    return sqlite3.connect("integrity_db.db")


def select_algorithms(connection: sqlite3.Connection) -> List[str]:
    """
    Запрос списка алгоритмов расчёта контрольных сумм.
    :param connection:
    :return:
    """
    try:
        query = connection.execute("SELECT name FROM algorithms ORDER BY id;")
        return [row[0] for row in query.fetchall()]
    except sqlite3.OperationalError:
        raise ParamError("Не удалось выполнить запрос")


def insert_into_aux_table(
    connection: sqlite3.Connection, table: str, fields: List[str], values: List[str]
):
    """
    Добавление записи в таблицу вспомогательной базы данных.
    :param connection:
    :param table:
    :param fields:
    :param values:
    :return:
    """
    try:
        if len(fields) != len(values):
            raise ParamError("Число полей не совпадает с числом значений")
    except TypeError:
        raise ParamTypeError("Список полей и список значений должны иметь тип list")
    try:
        fields_str = ", ".join([f'"{field}"' for field in fields])
        values_str = ", ".join([f"'{value}'" for value in values])
        connection.execute(
            f'INSERT INTO "{table}" ({fields_str}) VALUES ({values_str});'
        )
    except sqlite3.OperationalError:
        raise DatabaseError("Не удалось добавить запись")


def get_algorithm_id(connection: sqlite3.Connection, name: str) -> int:
    """
    Определяет id алгоритма во вспомогательной базе данных по его названию.
    :param connection:
    :param name:
    :return:
    """
    try:
        query = connection.execute("SELECT id FROM algorithms WHERE name = ?;", (name,))
        res = query.fetchone()
        if not res:
            raise ParamError("Алгоритм не найден")
        return res[0]
    except sqlite3.OperationalError:
        raise DatabaseError("Не удалось выполнить запрос")


def get_database_id(connection: sqlite3.Connection, name: str) -> int:
    """
    Определяет id защищаемой БД во вспомогательной БД по названию её соединения.
    :param connection:
    :param name:
    :return:
    """
    try:
        query = connection.execute(
            "SELECT id FROM databases WHERE connection = ?;", (name,)
        )
        res = query.fetchone()
        if not res:
            raise ParamError("База данных не найдена")
        return res[0]
    except sqlite3.OperationalError:
        raise DatabaseError("Не удалось выполнить запрос")


def get_reference_checksum(
    connection: sqlite3.Connection, table: str, table_fields: Tuple, query_params: Dict
):
    """
    Запрос эталонной контрольной суммы заданного объекта.
    :param connection:
    :param table:
    :param table_fields:
    :param query_params:
    :return:
    """
    try:
        params_str = " AND ".join([f't."{field}" = ?' for field in query_params])
        fields_str = ", ".join([f't."{field}"' for field in table_fields])
        query = connection.execute(
            f'SELECT {fields_str}, a.name FROM "{table}" t '
            "INNER JOIN algorithms a ON a.id = t.algorithm_id "
            f"WHERE {params_str};",
            tuple(query_params.values()),
        )
        res = query.fetchone()
        if not res:
            raise ParamError("Запись не найдена")
        return res
    except sqlite3.OperationalError:
        raise DatabaseError("Не удалось выполнить запрос")


def delete_from_aux_table(
    connection: sqlite3.Connection, table: str, query_params: Dict
):
    """
    Удаление записи из таблицы вспомогательной БД
    :param connection:
    :param table:
    :param query_params:
    :return:
    """
    try:
        params_str = " AND ".join([f'"{field}" = ?' for field in query_params])
        connection.execute(
            f'DELETE FROM "{table}" WHERE {params_str};', tuple(query_params.values())
        )
    except sqlite3.OperationalError as e:
        raise DatabaseError("Не удалось выполнить запрос")


def select_incorrect(
    connection: sqlite3.Connection,
    table: str,
    field: str,
    join_field: str,
    limit: int = None,
    offset: int = None,
):
    """
    Запрос списка объектов с нарушенной целостностью
    из таблицы вспомогательной БД
    :param connection:
    :param table:
    :param field:
    :param join_field:
    :param limit:
    :param offset:
    :return:
    """
    try:
        limit_str = f" LIMIT {limit}" if limit else ""
        offset_str = f" OFFSET {offset}" if offset else ""
        if table == "files":
            join_table = "file_errors"
        elif table == "tables":
            join_table = "table_errors"
        else:
            raise ParamError("Указана неправильная таблица")
        query = connection.execute(
            f'SELECT t."{field}", MAX(e.checked_at) FROM "{table}" t '
            f'INNER JOIN {join_table} e on e."{join_field}" = t.id '
            'WHERE NOT t."is_correct" '
            f'ORDER BY t."id"{limit_str}{offset_str};',
        )
        res = query.fetchall()
        if not res:
            raise ParamError("Записи не найдены")
        return res
    except sqlite3.OperationalError:
        raise DatabaseError("Не удалось выполнить запрос")


def mark_as_incorrect(connection: sqlite3.Connection, table: str, pk: int):
    """
    Отметка объекта защиты как имеющего нарушение целостности
    :param connection:
    :param table:
    :param pk:
    :return:
    """
    try:
        connection.execute(f"UPDATE {table} SET is_correct = 0 WHERE id = {pk};")
    except sqlite3.OperationalError:
        raise DatabaseError("Не удалось выполнить запрос")


def select_count_aux(connection: sqlite3.Connection, table: str) -> int:
    """
    Запрос числа записей в таблице вспомогательной базы данных.
    :param connection:
    :param table:
    :return:
    """
    try:
        query = connection.execute(f'SELECT COUNT(*) cnt FROM "{table}";')
        return query.fetchone()[0]
    except sqlite3.OperationalError:
        raise ParamError("Не удалось выполнить запрос")


def select_from_aux_db(
    connection: sqlite3.Connection,
    table: str,
    name_field: str,
    fk_field: str,
    errors_table: str,
    limit: int,
    offset: int,
    database_id: int = None,
    only_incorrect: bool = False,
):
    try:
        where_clause = f"WHERE t.database_id = {database_id} " if database_id else ""
        if only_incorrect:
            where_clause += "AND " if where_clause else "WHERE "
            where_clause += "NOT is_correct "
        query = connection.execute(
            f"SELECT t.{name_field}, a.name, t.checksum, "
            f't.calculated_at, MAX(e.checked_at), t.id FROM "{table}" t '
            f'LEFT OUTER JOIN "{errors_table}" e ON e.{fk_field} = t.id '
            f'INNER JOIN "algorithms" a ON a.id = t.algorithm_id {where_clause}'
            f"GROUP BY t.id ORDER BY t.id LIMIT {limit} OFFSET {offset};"
        )
        return query.fetchall()
    except sqlite3.OperationalError:
        raise ParamError("Не удалось выполнить запрос")


def select_watched_files(connection: sqlite3.Connection):
    try:
        query = connection.execute("SELECT path FROM files WHERE is_watched IS true;")
        return [row[0] for row in query.fetchall()]
    except sqlite3.OperationalError:
        return []


def select_file_id(connection: sqlite3.Connection, path: str):
    try:
        query = connection.execute(f"SELECT id FROM files WHERE path = '{path}';")
        return query.fetchone()[0]
    except sqlite3.OperationalError:
        raise ParamError("Файл не найден")


# Работа с защищаемой БД


def connect_to_db(
    connection_string: str, aux_connection: sqlite3.Connection
) -> Connectable:
    """
    Обеспечивает соединение с защищаемой базой данных.
    Если это первое соединение с ней, регистрирует его во вспомогательной БД.
    :param aux_connection:
    :param connection_string:
    :return: объект соединения
    """
    engine = create_engine(connection_string)
    try:
        connection_name = get_connection_name(connection_string)
        try:
            get_database_id(aux_connection, connection_name)
        except ParamError:
            aux_connection.execute(
                "INSERT INTO databases (connection) VALUES (?)", (connection_name,)
            )
        return engine.connect()
    except (OperationalError, ProgrammingError):
        raise ParamError("Не удалось соединиться с указанной базой данных")


def select_count(connection: Connectable, table: str) -> int:
    """
    Запрос числа записей в таблице защищаемой базы данных.
    :param connection:
    :param table:
    :return:
    """
    try:
        q = '"' if connection.engine.url.drivername == "postgresql" else "`"
        query = connection.execute(f'SELECT COUNT(*) cnt FROM {q}{table}{q};')
        return query.fetchone()[0]
    except (OperationalError, ProgrammingError):
        raise ParamError("Не удалось выполнить запрос")


def select_all_from_table(connection: Connectable, table: str, pk_field: str) -> str:
    """
    Запрос всех записей из таблицы защищаемой базы данных.
    Каждое поле приводится к строковому типу, и все данные конкатенируются.
    :param connection:
    :param table:
    :param pk_field:
    :return:
    """
    try:
        q = '"' if connection.engine.url.drivername == "postgresql" else "`"
        query = connection.execute(f'SELECT * FROM {q}{table}{q} ORDER BY "{pk_field}";')
        return "".join(
            ["".join([str(field) for field in row]) for row in query.fetchall()]
        )
    except (OperationalError, ProgrammingError):
        raise ParamError("Не удалось выполнить запрос")
