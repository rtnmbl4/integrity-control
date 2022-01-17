import os
import sqlite3
import sys
from datetime import datetime

from PyQt5.QtCore import QEvent, QObject, QThread, Qt, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QColor, QKeyEvent
from PyQt5.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidgetItem,
    QWidget,
)

import integrity_lib as ilib
import integrity_repl as irepl
from gui.ui_add_file import Ui_AddFileDialog
from gui.ui_add_table import Ui_AddTableDialog
from gui.ui_db_connect import Ui_DBConnectDialog
from gui.ui_main import Ui_MainWindow
from gui.ui_table_actions import Ui_TableActions


class ScriptExecutor(QObject):
    finished = pyqtSignal()
    commandComplete = pyqtSignal(str)

    def __init__(self, _command_list):
        super().__init__()
        self.command_list = _command_list

    def run(self):
        repl = irepl.REPL()
        for line in self.command_list:
            command = line.split()
            if not command:
                continue
            operator = command[0]
            if operator == "exit":
                self.ui.scriptResult.appendPlainText("Выход")
                break
            args = irepl.parse_quotes(command[1:])
            if operator in irepl.COMMANDS:
                self.commandComplete.emit(getattr(repl, operator)(*args))
                if repl.error:
                    self.commandComplete.emit(
                        "Выполнение скрипта прекращено из-за ошибки"
                    )
                    break
            else:
                self.commandComplete.emit(
                    f'Неправильная команда "{operator}"; выполнение скрипта прекращено'
                )
                break
        self.finished.emit()


class MainWindow(QMainWindow):
    def __init__(self):
        super(MainWindow, self).__init__()
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)
        self.aux_connection = ilib.connect_to_auxiliary_db()
        self.aux_connection.execute("PRAGMA foreign_keys = ON;")
        self.connection = None
        self.info_label_template = "Страница {} из {}. Всего записей: {}"
        number_of_files = ilib.select_count_aux(self.aux_connection, "files")
        self.table_config = (
            {
                "table": "files",
                "errors_table": "file_errors",
                "widget": self.ui.tableFiles,
                "total": number_of_files,
                "total_pages": int((number_of_files - 1) / 10) + 1,
                "current_page": 0,
                "page_size": 10,
                "info_widget": self.ui.filesInfo,
                "name_field": "path",
                "fk_field": "file_id",
                "database_id": None,
                "id_list": None,
            },
            {
                "table": "tables",
                "errors_table": "table_errors",
                "widget": self.ui.tableTables,
                "total": 0,
                "total_pages": 0,
                "current_page": 0,
                "page_size": 10,
                "info_widget": self.ui.tablesInfo,
                "name_field": "table_name",
                "fk_field": "table_id",
                "database_id": None,
                "id_list": None,
            },
        )
        if int(number_of_files / 10 - 1) == 0:
            self.ui.filesForwardButton.setDisabled(True)
        self._set_table_data(0)
        self.ui.scriptEdit.installEventFilter(self)
        self.ui.tableFiles.installEventFilter(self)
        self.ui.tableTables.installEventFilter(self)

    def _execute_script(self):
        self.ui.scriptResult.clear()
        self.ui.execute.setDisabled(True)
        script = self.ui.scriptEdit.toPlainText()
        self.thread = QThread()
        self.executor = ScriptExecutor(script.split("\n"))
        self.executor.moveToThread(self.thread)
        self.thread.started.connect(self.executor.run)
        self.executor.finished.connect(self.thread.quit)
        self.executor.finished.connect(self.executor.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.executor.commandComplete.connect(self.updateScriptResult)
        self.thread.finished.connect(lambda: self.ui.execute.setEnabled(True))
        self.thread.start()

    @pyqtSlot(str)
    def updateScriptResult(self, msg: str):
        self.ui.scriptResult.appendPlainText(msg)

    def _set_table_data(self, index, only_incorrect=False):
        def readonly_item(content):
            item = QTableWidgetItem(content)
            item.setFlags(item.flags() ^ Qt.ItemIsEditable)
            return item

        t = self.table_config[index]
        try:
            raw_data = ilib.select_from_aux_db(
                self.aux_connection,
                t["table"],
                t["name_field"],
                t["fk_field"],
                t["errors_table"],
                t["page_size"],
                t["page_size"] * t["current_page"],
                only_incorrect=only_incorrect,
            )
            t["widget"].setRowCount(len(raw_data))
            t["id_list"] = []
            for i, row in enumerate(raw_data):
                t["widget"].setItem(i, 0, readonly_item(row[0]))
                t["widget"].setItem(i, 1, readonly_item(row[1]))
                t["widget"].setItem(i, 2, readonly_item(row[2]))
                t["widget"].setItem(
                    i,
                    3,
                    readonly_item(
                        datetime.fromtimestamp(row[3]).strftime("%d.%m.%Y %H:%M:%S UTC")
                    ),
                )
                t["widget"].setItem(
                    i,
                    4,
                    readonly_item(
                        datetime.fromtimestamp(row[4]).strftime("%d.%m.%Y %H:%M:%S UTC")
                        if row[4]
                        else ""
                    ),
                )
                t["widget"].setCellWidget(i, 5, TableActions(self, i))
                t["id_list"].append(row[-1])
            t["info_widget"].setText(
                self.info_label_template.format(
                    t["current_page"] + 1, t["total_pages"], t["total"]
                )
            )
        except ilib.IntegrityLibError as e:
            error_box = QMessageBox(self)
            error_box.setText(e.message)
            error_box.setWindowTitle("Ошибка запроса")
            error_box.show()

    def _change_page(self, sender, *, direction: int, counterpart: QPushButton):
        enable_sender_afterwards = True
        index = self.ui.tabs.currentIndex()
        t = self.table_config[index]
        t["current_page"] += direction
        if t["current_page"] == 0 or t["current_page"] == t["total_pages"] - 1:
            enable_sender_afterwards = False
        if t["total_pages"] > 1:
            counterpart.setEnabled(True)
        self._set_table_data(index)
        sender.setEnabled(enable_sender_afterwards)

    @pyqtSlot()
    def changePage(self):
        sender = self.sender()
        sender.setDisabled(True)
        if sender is self.ui.filesBackButton:
            self._change_page(
                sender, direction=-1, counterpart=self.ui.filesForwardButton
            )
        if sender is self.ui.tablesBackButton:
            self._change_page(
                sender, direction=-1, counterpart=self.ui.tablesForwardButton
            )
        if sender is self.ui.filesForwardButton:
            self._change_page(sender, direction=1, counterpart=self.ui.filesBackButton)
        if sender is self.ui.tablesForwardButton:
            self._change_page(sender, direction=1, counterpart=self.ui.tablesBackButton)

    def _table_cancel(self):
        self.aux_connection.rollback()
        for table in (self.ui.tableFiles, self.ui.tableTables):
            for i in range(table.rowCount()):
                for j in range(table.columnCount() - 1):
                    table.item(i, j).setBackground(QColor.fromRgb(255, 255, 255))
                table.cellWidget(i, table.columnCount() - 1).setBackgroundColor(
                    (255, 255, 255)
                )

    @pyqtSlot(int)
    def changeTab(self, index: int):
        self.ui.openScript.setEnabled(index == 2)
        self.ui.saveScript.setEnabled(index == 2)
        self.ui.execute.setEnabled(index == 2)
        self.ui.clearResults.setEnabled(index == 2)
        self.ui.addObject.setEnabled(
            index == 0 or (index == 1 and self.connection is not None)
        )
        self._table_cancel()

    @pyqtSlot()
    def handleAction(self):
        action = self.sender()
        if action is self.ui.connectToDB:
            dialog = ConnectToDatabaseDialog(self.aux_connection)
            dialog.exec_()
            if dialog.connection:
                self.connection = dialog.connection
                self.ui.headerTables.setText(
                    f"Текущая база данных: {self.connection.engine.url[5]}"
                    f"@{self.connection.engine.url[3]}"
                    f":{self.connection.engine.url[4]}"
                )
                self.table_config[1]["database_id"] = ilib.get_database_id(
                    self.aux_connection,
                    ilib.get_connection_name(
                        ilib.make_connection_string(*self.connection.engine.url[0:6])
                    ),
                )
                self.table_config[1]["current_page"] = 0
                self.table_config[1]["page_size"] = 10
                number_of_tables = ilib.select_count_aux(self.aux_connection, "tables")
                self.table_config[1]["total"] = number_of_tables
                self.table_config[1]["total_pages"] = (
                    int((number_of_tables - 1) / 10) + 1
                )
                self._set_table_data(1)
                self.ui.tablesCommitButton.setEnabled(True)
                self.ui.tablesPageSize.setEnabled(True)
                self.ui.tablesRollbackButton.setEnabled(True)
                self.ui.tablesForwardButton.setEnabled(
                    self.table_config[1]["total_pages"] > 1
                )
            else:
                self.ui.headerTables.setText(
                    "Текущая база данных: подключение отсутствует"
                )
                self.table_config[1]["database_id"] = None
                self.ui.tablesCommitButton.setDisabled(True)
                self.ui.tablesPageSize.setDisabled(True)
                self.ui.tablesBackButton.setDisabled(True)
                self.ui.tablesForwardButton.setDisabled(True)
                self.ui.tablesRollbackButton.setDisabled(True)
                self.ui.tableTables.clear()
        if action is self.ui.execute:
            self._execute_script()
        if action is self.ui.clearResults:
            self.ui.scriptResult.clear()
        if action is self.ui.addObject:
            if self.ui.tabs.currentIndex() == 0:
                dialog = AddFileDialog()
                dialog.exec_()
            if self.ui.tabs.currentIndex() == 1:
                con = self.connection.engine.url
                dialog = AddTableDialog(
                    (
                        con.host,
                        con.port,
                        con.database,
                        con.username,
                        con.password,
                        con.drivername.split("+")[0],
                    )
                )
                dialog.exec_()
            if dialog.message:
                msg_box = QMessageBox(self)
                msg_box.setText(dialog.message)
                msg_box.setWindowTitle("Результат добавления")
                msg_box.show()
            if dialog.success:
                self._set_table_data(self.ui.tabs.currentIndex())
                if self.ui.tabs.currentIndex() == 1:
                    self.ui.addObject.setEnabled(True)
        if action is self.ui.saveScript:
            file_path, _ = QFileDialog.getSaveFileName(
                self,
                "Сохранение скрипта",
                os.getcwd(),
                "Скрипты IntegrityLib (*.ils);;Все файлы (*.*)",
            )
            with open(file_path, "w") as file:
                file.write(self.ui.scriptEdit.toPlainText())
        if action is self.ui.openScript:
            file_path, _ = QFileDialog.getOpenFileName(
                self,
                "Открытие скрипта",
                os.getcwd(),
                "Скрипты IntegrityLib (*.ils);;Все файлы (*.*)",
            )
            with open(file_path) as file:
                self.ui.scriptEdit.setPlainText(file.read())

    @pyqtSlot()
    def changePageSize(self):
        sender = self.sender()
        value = sender.value()
        index = int(sender is self.ui.tablesPageSize)
        table_conf = self.table_config[index]
        if value != table_conf["page_size"]:
            table_conf["page_size"] = value
            table_conf["current_page"] = 0
            table_conf["total_pages"] = int((table_conf["total"] - 1) / value) + 1
            if index == 0:
                self.ui.filesForwardButton.setEnabled(table_conf["total_pages"] > 1)
            if index == 1:
                self.ui.tablesForwardButton.setEnabled(table_conf["total_pages"] > 1)
            self._set_table_data(index)

    def _delete_record(self, position):
        index = self.ui.tabs.currentIndex()
        t = self.table_config[index]
        pk = t["id_list"][position]
        ilib.delete_from_aux_table(self.aux_connection, t["table"], {"id": pk})
        for j in range(t["widget"].columnCount() - 1):
            t["widget"].item(position, j).setBackground(QColor.fromRgb(255, 63, 63))
        t["widget"].cellWidget(
            position, t["widget"].columnCount() - 1
        ).setBackgroundColor((255, 63, 63))

    def _check_record(self, position):
        index = self.ui.tabs.currentIndex()
        t = self.table_config[index]
        repl = irepl.REPL()
        if index == 1:
            repl.connection = self.connection
        result = repl.check(
            "file" if index == 0 else "table", t["widget"].item(position, 0).text()
        )
        msg_box = QMessageBox(self)
        if repl.last_check_no_error or index == 1:
            msg_box.setText(result)
        else:
            msg_box.setText(result + "\nВыполнить восстановление из резервной копии?")
            msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg_box.setWindowTitle("Результат проверки")
        res = msg_box.exec_()
        if res == QMessageBox.Yes:
            try:
                msg_box = QMessageBox(self)
                repl.restore(
                    "file" if index == 0 else "table",
                    t["widget"].item(position, 0).text(),
                )
                msg_box.setText("Восстановление завершено")
            except ilib.IntegrityLibError as e:
                msg_box.setText(e.message)
            msg_box.setStandardButtons(QMessageBox.Ok)
            msg_box.setWindowTitle("Результат восстановления")
            msg_box.show()

    @pyqtSlot()
    def handleTableClick(self):
        sender = self.sender()
        if sender is sender.parent().ui.removeButton:
            self._delete_record(sender.parent().position)
        if sender is sender.parent().ui.checkButton:
            self._check_record(sender.parent().position)

    @pyqtSlot()
    def handleRollback(self):
        self._table_cancel()

    @pyqtSlot()
    def handleCommit(self):
        self.aux_connection.commit()
        self._set_table_data(self.ui.tabs.currentIndex())

    def eventFilter(self, sender, event):
        if (
            sender is self.ui.scriptEdit
            and isinstance(event, QKeyEvent)
            and event.type() == QEvent.KeyPress
            and QApplication.keyboardModifiers() == Qt.ControlModifier
            and event.key() == Qt.Key_Return
        ):
            self._execute_script()
        if (
            (sender is self.ui.tableFiles or sender is self.ui.tableTables)
            and isinstance(event, QKeyEvent)
            and event.type() == QEvent.KeyPress
            and event.key() == Qt.Key_Delete
        ):
            for row in sender.selectedItems():
                self._delete_record(row.row())
        return sender.eventFilter(sender, event)

    @pyqtSlot(int)
    def filterTable(self, state: int):
        self.aux_connection.rollback()
        index = self.ui.tabs.currentIndex()
        do_filter = state == Qt.Checked
        self._set_table_data(index, do_filter)


class ConnectToDatabaseDialog(QDialog):
    def __init__(self, _aux_connection: sqlite3.Connection):
        super(ConnectToDatabaseDialog, self).__init__()
        self.ui = Ui_DBConnectDialog()
        self.ui.setupUi(self)
        self.aux_connection = _aux_connection
        self.connection = None

    def accept(self):
        try:
            dbms = self.ui.dbmsInput.currentText()
            login = self.ui.userInput.text()
            password = self.ui.passwordInput.text()
            host = self.ui.hostInput.text()
            port = self.ui.portInput.value()
            database = self.ui.dbInput.text()
            if not all([dbms, login, password, host, port, database]):
                raise ilib.ParamError(
                    "Указаны не все параметры для соединения с базой данных!"
                )
            self.connection = ilib.connect_to_db(
                ilib.make_connection_string(
                    dbms.lower(),
                    login,
                    password,
                    host,
                    str(port),
                    database,
                ),
                self.aux_connection,
            )
            super().accept()
        except ilib.IntegrityLibError as e:
            error_box = QMessageBox(self)
            error_box.setText(e.message)
            error_box.setWindowTitle("Ошибка соединения")
            error_box.show()


class TableActions(QWidget):
    def __init__(self, parent=None, _position: int = None):
        super(TableActions, self).__init__(parent)
        self.ui = Ui_TableActions()
        self.ui.setupUi(self)
        self.ui.checkButton.clicked.connect(parent.handleTableClick)
        self.ui.removeButton.clicked.connect(parent.handleTableClick)
        self.position = _position

    def setBackgroundColor(self, rgb):
        try:
            red, green, blue = rgb
            self.setStyleSheet(f"background-color: rgb({red}, {green}, {blue});")
        except (ValueError, TypeError):
            self.setStyleSheet("background-color: white;")


class AddFileDialog(QDialog):
    def __init__(self):
        super(AddFileDialog, self).__init__()
        self.ui = Ui_AddFileDialog()
        self.ui.setupUi(self)
        self.repl = irepl.REPL()
        self.ui.algorithmInput.addItems(
            ilib.select_algorithms(self.repl.aux_connection)
        )
        self.success = False
        self.message = None

    @pyqtSlot()
    def showFileDialog(self):
        file_name, _ = QFileDialog.getOpenFileName(
            self, "Выбор файла", os.getcwd(), "Все файлы (*.*)"
        )
        self.ui.filePathInput.setText(file_name)

    def accept(self):
        opt_args = []
        if self.ui.watchInput.isChecked():
            opt_args.append("watch")
        if self.ui.backupInput.isChecked():
            opt_args.append("backup")
        self.message = self.repl.add(
            self.ui.algorithmInput.currentText(),
            "file",
            self.ui.filePathInput.text(),
            *opt_args,
        )
        self.success = not self.repl.error
        super().accept()


class AddTableDialog(QDialog):
    def __init__(self, connection_params: tuple):
        super(AddTableDialog, self).__init__()
        self.ui = Ui_AddTableDialog()
        self.ui.setupUi(self)
        self.repl = irepl.REPL()
        self.ui.algorithmInput.addItems(
            ilib.select_algorithms(self.repl.aux_connection)
        )
        self.repl.db_connect(*connection_params)
        self.success = False
        self.message = None

    @pyqtSlot()
    def testTable(self):
        msg_box = QMessageBox(self)
        try:
            ilib.select_count(self.repl.connection, self.ui.tableInput.text())
            msg_box.setText("Таблица существует")
        except ilib.IntegrityLibError:
            msg_box.setText("Таблица не существует")
        msg_box.setWindowTitle("Результат проверки")
        msg_box.show()

    def accept(self):
        self.message = self.repl.add(
            self.ui.algorithmInput.currentText(),
            "table",
            self.ui.tableInput.text(),
            self.ui.pkInput.text(),
        )
        self.success = not self.repl.error
        super().accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())
