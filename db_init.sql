PRAGMA foreign_keys = ON;

CREATE TABLE algorithms(
    id   INTEGER PRIMARY KEY,
    name TEXT
);

INSERT INTO algorithms (name) VALUES
('blake2b'),
('blake2s'),
('shake_256'),
('sha3_256'),
('sha1'),
('shake_128'),
('sha384'),
('sha512'),
('sha3_512'),
('sha3_224'),
('sha3_384'),
('md5'),
('sha256'),
('sha224'),
('crc32'),
('crc64'),
('adler32'),
('gost_256'),
('gost_512'),
('gost94');

CREATE TABLE files(
    id            INTEGER PRIMARY KEY,
    path          TEXT,
    checksum      TEXT,
    algorithm_id  INTEGER,
    file_size     INTEGER,
    is_watched    INTEGER,
    is_correct    INTEGER,
    calculated_at INTEGER,
    FOREIGN KEY(algorithm_id) REFERENCES algorithms(id) ON DELETE CASCADE
);

CREATE TABLE file_errors(
    id         INTEGER PRIMARY KEY,
    file_id    INTEGER,
    checked_at INTEGER,
    manual     INTEGER,
    FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
);

CREATE TABLE databases(
    id         INTEGER PRIMARY KEY,
    connection TEXT
);

CREATE TABLE tables(
    id            INTEGER PRIMARY KEY,
    database_id   INTEGER,
    table_name    TEXT,
    checksum      TEXT,
    algorithm_id  INTEGER,
    row_count     INTEGER,
    is_correct    INTEGER,
    calculated_at INTEGER,
    pk_field      TEXT,
    FOREIGN KEY(database_id) REFERENCES databases(id) ON DELETE CASCADE,
    FOREIGN KEY(algorithm_id) REFERENCES algorithms(id) ON DELETE CASCADE
);

CREATE TABLE table_errors(
    id         INTEGER PRIMARY KEY,
    table_id   INTEGER,
    checked_at INTEGER,
    FOREIGN KEY(table_id) REFERENCES tables(id) ON DELETE CASCADE
);
