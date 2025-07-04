import subprocess
import string
import re
import pathlib
import json
from datetime import datetime, timedelta
import time
from collections import Counter, deque
from copy import deepcopy
from sys import platform
from tempfile import gettempdir, TemporaryDirectory


from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtGui import (QBrush, QColor, QTextCursor, QFont, QFontMetrics, QTextDocument, 
QCursor, QStandardItem, QStandardItemModel, QPageSize, QTextBlock, QTextFormat, QTextBlockFormat, 
QTextOption, QTextCharFormat, QKeySequence, QPalette, QDesktopServices, QPixmap, QAction, QIcon)
from PySide6.QtWidgets import (QMainWindow, QFileDialog, QInputDialog, QListWidgetItem, QTableWidgetItem, 
QStyle, QMessageBox, QDialog, QFontDialog, QColorDialog, QLabel, QMenu, 
QCompleter, QApplication, QTextEdit, QPlainTextEdit, QProgressBar,  QToolButton, QDockWidget)
from PySide6.QtMultimedia import (QMediaPlayer, QMediaRecorder, QMediaFormat)
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtCore import Qt, QFile, QTextStream, QUrl, QTime, QDateTime, QSettings, QRegularExpression, QSize, QStringListModel, QSizeF, QTimer, QThread
_ = lambda txt: QtCore.QCoreApplication.translate("Plover2CAT", txt)

import plover

from plover.engine import StenoEngine
from plover.steno import Stroke, normalize_steno, normalize_stroke
from plover.dictionary.base import load_dictionary
from plover.system.english_stenotype import DICTIONARIES_ROOT, ORTHOGRAPHY_WORDLIST
from plover.system import _load_wordlist
from plover import log

from plover_cat.__version__ import __version__

from plover_cat.plover_cat_ui import Ui_PloverCAT
from plover_cat.TextEditor import PloverCATEditor
from plover_cat.fieldDialogWindow import fieldDialogWindow
from plover_cat.affixDialogWindow import affixDialogWindow
from plover_cat.shortcutDialogWindow import shortcutDialogWindow
from plover_cat.indexDialogWindow import indexDialogWindow
from plover_cat.suggestDialogWindow import suggestDialogWindow
from plover_cat.captionDialogWindow import captionDialogWindow
from plover_cat.recorderDialogWindow import recorderDialogWindow
from plover_cat.testDialogWindow import testDialogWindow

from plover_cat.rtf_parsing import *
from plover_cat.constants import *
from plover_cat.qcommands import *
from plover_cat.helpers import * 
from plover_cat.steno_objects import *
from plover_cat.spellcheck import *
from plover_cat.export_helpers import * 
from plover_cat.FlowLayout import FlowLayout
from plover_cat.documentWorker import documentWorker
from plover_cat.captionWorker import captionWorker

scowl = _load_wordlist(ORTHOGRAPHY_WORDLIST, DICTIONARIES_ROOT)

try:
    from os import startfile
except:
    pass


class PloverCATWindow(QMainWindow, Ui_PloverCAT):
    """Main window that can hold multiple transcripts.

    This manages the main window, the dialog and global settings.
    Connections should be made from this class to each transcript  ``PloverCATEditor``
    which is displayed as a tab in center widget.

    Generally, actions performed on the transcript should call a transcript method,
    and the transcript should not be modified directly.

    Any action that needs to be undo-able must use a transcript method.

    The ``__init__`` method sets up the window, summarized.

        - Access settings for window settings (dock, layout) from last time

        - Connect menu items with methods but enable only needed

        - Connect Plover engine hooks

        - Create empty dialogs

    :ivar engine: Plover engine instance
    :ivar video: holds a ``QVideoWidget`` if one exists for media in transcript
    :ivar textEdit: hold current transcript 
    :ivar index_dialog: instance of ``indexDialogWindow``
    :ivar caption_dialog: instance of ``captionDialogWindow``
    :ivar suggest_dialog: instance of ``suggestDialogWindow``
    :ivar cap_worker: instance of ``capWorker``
    :ivar autosave_time: autosave timer as ``QTimer``
    :ivar cutcopy_storage: deque, length 5, holding date for copy/pasting
    :ivar thread: thread for non-main thread processing
    :ivar progressBar: instance of ``QProgressBar`` for display
    :ivar caption_cursor_pos: integer position of text last sent to display

    """
    def __init__(self, engine):
        super().__init__()
        self.engine = engine
        # ui and widgets setup
        self.setupUi(self)
        self.recentfileflow = FlowLayout()
        self.recentfileflow.setSizeConstraint(QtWidgets.QLayout.SetDefaultConstraint)
        self.recentfileflow.setObjectName("recentfileflow")
        self.flowparent.addLayout(self.recentfileflow)
        self.flowparent.addStretch()
        self.video = None
        self.dock_status = {}
        for doc in self.findChildren(QDockWidget):
            self.dock_status[doc.objectName()] = False
            doc.visibilityChanged.connect(lambda status, dock = doc.objectName(): self.dock_handler(status, dock))
            doc.visibilityChanged.connect(lambda status: self.update_gui())
        # vars for startup
        ## on very first startup, set tabs up
        ## later configs will use window settings
        self.tabifyDockWidget(self.dockHistoryStack, self.dockPaper)
        self.tabifyDockWidget(self.dockPaper, self.dockNavigation)
        self.tabifyDockWidget(self.dockAudio, self.dockStenoData)
        settings = QSettings("Plover2CAT", "OpenCAT")
        if settings.contains("geometry"):
            self.restoreGeometry(settings.value("geometry"))
        if settings.contains("windowstate"):
            self.restoreState(settings.value("windowstate"))
        if settings.contains("windowfont"):
            font_string = settings.value("windowfont")
            font = QFont()
            font.fromString(font_string)
            self.setFont(font)
        if settings.contains("tapefont"):
            font_string = settings.value("tapefont")
            font = QFont()
            font.fromString(font_string)
            self.strokeList.setFont(font)
        if settings.contains("backgroundcolor"):
            back_color = QColor(settings.value("backgroundcolor"))
            window_pal = self.palette()
            window_pal.setColor(QPalette.Base, back_color)
            self.setPalette(window_pal)
        if settings.contains("suggestionsource"):
            self.suggest_source.setCurrentIndex(settings.value("suggestionsource"))
        if settings.contains("recentfiles"):
            self.recent_file_menu()
        self.textEdit = None
        self.test_dialog = None
        self.index_dialog = indexDialogWindow({})
        self.caption_dialog = captionDialogWindow() 
        self.suggest_dialog = suggestDialogWindow(None, self.engine, scowl)
        self.cap_worker = None
        self.autosave_time = QTimer()
        self.cutcopy_storage = deque(maxlen = 5)
        self.thread = QThread()
        self.progressBar = QProgressBar()
        self.caption_cursor_pos = 0
        self.actionUndo = None
        self.actionRedo = None
        self.menu_enabling()
        self.audio_menu_enabling(False)
        self.set_shortcuts()
        self.update_highlight_color()
        # connections:
        ## engine connections
        engine.signal_connect("stroked", self.on_stroke) 
        engine.signal_connect("stroked", self.log_to_tape) 
        engine.signal_connect("send_string", self.on_send_string)
        engine.signal_connect("send_backspaces", self.count_backspaces)     
        ## file setting/saving
        self.actionQuit.triggered.connect(lambda: self.action_close())
        self.actionOpen.triggered.connect(lambda: self.open_file())
        self.actionNew.triggered.connect(lambda: self.create_new())
        self.actionClose.triggered.connect(lambda: self.close_file())
        self.actionSave.triggered.connect(lambda: self.save_file())
        self.mainTabs.tabCloseRequested.connect(self.close_file)
        self.actionSaveAs.triggered.connect(lambda: self.save_as_file())
        self.menuRecentFiles.triggered.connect(self.recentfile_open)
        self.actionEnableAutosave.triggered.connect(self.autosave_setup)
        self.actionSetAutosaveTime.triggered.connect(self.set_autosave_time)
        self.autosave_time.timeout.connect(self.autosave)
        self.actionOpenTranscriptFolder.triggered.connect(lambda: self.open_root())
        self.actionImportRTF.triggered.connect(lambda: self.import_rtf())
        ## audio connections
        self.actionOpenAudio.triggered.connect(lambda: self.open_audio())
        self.actionRecordPause.triggered.connect(lambda: self.record_or_pause())
        self.actionStopRecording.triggered.connect(lambda: self.stop_record())
        self.actionShowVideo.triggered.connect(lambda: self.show_hide_video())
        self.actionCaptioning.triggered.connect(self.setup_captions)
        self.actionFlushCaption.triggered.connect(self.flush_caption)
        # self.actionAddChangeAudioTimestamps.triggered.connect(self.modify_audiotime)
        ## editor related connections
        self.actionClearParagraph.triggered.connect(lambda: self.reset_paragraph())
        self.actionCopy.triggered.connect(lambda: self.cut_steno(cut = False))
        self.actionCut.triggered.connect(lambda: self.cut_steno())
        self.actionPaste.triggered.connect(lambda: self.paste_steno())
        self.menuClipboard.triggered.connect(self.paste_steno)
        self.actionJumpToParagraph.triggered.connect(self.jump_par)
        self.navigationList.itemDoubleClicked.connect(self.heading_navigation)
        self.actionRevertTranscript.triggered.connect(self.revert_file)
        ## insert related
        self.actionInsertImage.triggered.connect(lambda: self.insert_image())
        self.actionInsertNormalText.triggered.connect(self.insert_text)
        self.actionEditFields.triggered.connect(self.edit_fields)
        self.menuField.triggered.connect(self.insert_field)
        self.reveal_steno_refresh.clicked.connect(self.refresh_steno_display)
        self.actionAutomaticAffixes.toggled.connect(self.enable_affix)
        self.actionEditAffixes.triggered.connect(self.edit_auto_affixes)
        self.menuIndexEntry.triggered.connect(lambda action, el = None: self.insert_index_entry(el = el, action = action))
        self.actionEditIndices.triggered.connect(self.edit_indices)
        # self.actionRedact.triggered.connect(self.insert_redacted)     
        ## steno related edits
        self.actionMergeParagraphs.triggered.connect(lambda: self.textEdit.merge_paragraphs() if self.textEdit else None)
        self.actionSplitParagraph.triggered.connect(lambda: self.textEdit.split_paragraph() if self.textEdit else None)
        self.actionRetroactiveDefine.triggered.connect(lambda: self.define_retroactive())
        self.actionDefineLast.triggered.connect(lambda: self.define_scan())
        self.actionDeleteLast.triggered.connect(lambda: self.delete_scan())
        self.actionAutocompletion.triggered.connect(self.setup_completion)
        self.actionAddAutocompletionTerm.triggered.connect(self.add_autocomplete_item)
        self.actionTranslateTape.triggered.connect(self.tape_translate)
        ## dict related
        self.actionAddCustomDict.triggered.connect(lambda: self.add_dict())
        self.actionRemoveTranscriptDict.triggered.connect(lambda: self.remove_dict())
        self.actionTranscriptSuggestions.triggered.connect(lambda: self.transcript_suggest())
        ## style connections
        self.editCurrentStyle.clicked.connect(self.style_edit)
        self.actionCreateNewStyle.triggered.connect(self.create_new_style)
        self.actionRefreshEditor.triggered.connect(self.refresh_editor_styles)
        self.actionStyleFileSelect.triggered.connect(self.select_style_file)
        self.actionGenerateStyleFromTemplate.triggered.connect(self.style_from_template)
        self.blockFont.currentFontChanged.connect(self.calculate_space_width)
        ## view
        self.actionWindowFont.triggered.connect(lambda: self.change_window_font())
        self.actionBackgroundColor.triggered.connect(lambda: self.change_backgrounds())
        self.actionShowAllCharacters.triggered.connect(lambda: self.show_invisible_char())
        self.actionPaperTapeFont.triggered.connect(lambda: self.change_tape_font())
        self.menuHighlightColors.triggered.connect(self.change_highlight_colors)
        ## tools
        self.actionStyling.triggered.connect(lambda: self.show_toolbox_pane(self.styling_pane))
        self.actionPageFormat.triggered.connect(lambda: self.show_toolbox_pane(self.page_format_pane))
        self.actionFindReplacePane.triggered.connect(lambda: self.show_find_replace())
        self.actionParagraph.triggered.connect(lambda: self.show_toolbox_pane(self.paragraph_pane))
        self.actionSpellcheck.triggered.connect(lambda: self.show_toolbox_pane(self.spellcheck_pane))
        self.actionStenoSearch.triggered.connect(lambda: self.show_stenospell())
        self.actionSearchWikipedia.triggered.connect(lambda: self.search_online("https://en.wikipedia.org/wiki/Special:Search/{0}"))
        self.actionSearchMerriamWebster.triggered.connect(lambda: self.search_online("http://www.merriam-webster.com/dictionary/{0}"))
        self.actionSearchOED.triggered.connect(lambda: self.search_online("https://www.oed.com/search/dictionary/?scope=Entries&q={0}"))
        self.actionSearchGoogle.triggered.connect(lambda: self.search_online("https://www.google.com/search?q={0}"))
        self.actionSearchDuckDuckGo.triggered.connect(lambda: self.search_online("https://duckduckgo.com/?q={0}"))
        ## search/replace connections
        self.search_text.toggled.connect(lambda: self.search_text_options())
        self.search_steno.toggled.connect(lambda: self.search_steno_options())
        self.search_untrans.toggled.connect(lambda: self.search_untrans_options())
        self.searchResults.itemDoubleClicked.connect(self.search_navigation)
        ## spellcheck
        ## steno search
        self.steno_spellcheck.clicked.connect(lambda: self.spell_steno())
        ## suggestions
        self.suggest_sort.toggled.connect(lambda: self.get_suggestions())
        self.suggest_source.currentIndexChanged.connect(lambda: self.get_suggestions())
        ## tape
        self.strokeLocate.clicked.connect(lambda: self.stroke_to_text_move())
        ## export
        self.actionPlainText.triggered.connect(lambda: self.export_text())
        self.actionASCII.triggered.connect(lambda: self.export_ascii())
        self.actionPlainASCII.triggered.connect(lambda: self.export_plain_ascii())
        self.actionHTML.triggered.connect(lambda: self.export_html())
        self.actionSubRip.triggered.connect(lambda: self.export_srt())
        self.actionODT.triggered.connect(lambda: self.export_odt())
        self.actionRTF.triggered.connect(lambda: self.export_rtf())
        self.actionTape.triggered.connect(lambda: self.export_tape())
        # help
        self.actionUserManual.triggered.connect(lambda: self.open_help())
        self.actionAbout.triggered.connect(lambda: self.about())
        self.actionAcknowledgements.triggered.connect(lambda: self.acknowledge())
        self.actionEditMenuShortcuts.triggered.connect(self.edit_shortcuts)
        self.actionViewPloverLog.triggered.connect(self.view_log)
        self.actionRunTests.triggered.connect(self.open_tester)
        # status bar
        self.cursor_status = QLabel("Par,Char: {line},{char}".format(line = 0, char = 0))
        self.cursor_status.setObjectName("cursor_status")
        self.statusBar.addPermanentWidget(self.cursor_status)
        self.display_message("Create New Transcript or Open Existing...")

    def set_shortcuts(self):
        """Set shortcuts for menu items using keysequence strings.

        JSON file should be called ``shortcuts.json`` under a ``plover2cat`` file in the Plover home directory
        """
        shortcut_file = pathlib.Path(plover.oslayer.config.CONFIG_DIR) / "plover2cat" / "shortcuts.json"
        if not shortcut_file.exists():
            log.debug("No shortcut file exists, using default menu shortcuts.")
            return
        else:
            with open(shortcut_file, "r") as f:
                shortcuts = json.loads(f.read())
        log.debug(f"Shortcuts: {shortcuts}")
        for identifier, keysequence in shortcuts.items():
            try:
                select_action = self.findChild(QAction, identifier)
                select_action.setShortcut(QKeySequence(keysequence)) 
            except:
                pass  

    def edit_shortcuts(self):
        """Edit menu shortcuts from dialog.

        ``shortcuts.json`` will be created if it does not exist.
        """
        shortcut_dict = {}
        menu_names = []
        action_names = []
        for act in self.findChildren(QAction):
            txt = act.text()
            name = act.objectName()
            if txt and name:
                shortcut_dict[name] = act.shortcut().toString()
                menu_names.append(txt)
                action_names.append(name)
        self.shortcut_dialog = shortcutDialogWindow(shortcut_dict, menu_names, action_names)
        res = self.shortcut_dialog.exec_()
        if res:
            shortcut_file = pathlib.Path(plover.oslayer.config.CONFIG_DIR) / "plover2cat" / "shortcuts.json"
            if not shortcut_file.exists():
                save_json({}, shortcut_file)
            with open(shortcut_file, "r") as f:
                shortcuts = json.loads(f.read())
                shortcuts.update(self.shortcut_dialog.shortcut_dict)
            save_json(remove_empty_from_dict(shortcuts), shortcut_file)
            self.set_shortcuts()

    def about(self):
        """Display version info in dialog.
        """
        log.debug("User activated 'About' dialog.")
        QMessageBox.about(self, "Plover2CAT",
                "This is Plover2CAT version %s, a computer aided transcription plugin for Plover." % __version__)

    def acknowledge(self):
        """Display acknowledgements in dialog.
        """
        log.debug("User activated 'Acknowledgements' dialog.")
        QMessageBox.about(self, "Plover2CAT",
                        "Plover2CAT is built on top of Plover, the open source stenotype engine. "
                        "It owes its development to the members of the Plover discord group who provided suggestions and bug finding. "
                        "PySide6 is licensed under the LGPL and Plover is licensed under the GPL. "
                        "Fugue icons are by Yusuke Kamiyamane, under the Creative Commons Attribution 3.0 License.")

    def open_help(self):
        """Link to Readthedocs help pages.
        """
        log.debug("User activated 'User Manual' link.")
        user_manual_link = QUrl("https://plover2cat.readthedocs.io/en/latest/")
        QtGui.QDesktopServices.openUrl(user_manual_link)

    def view_log(self):
        self.open_tester()
        self.test_dialog.display_log()

    def open_tester(self):
        if not self.test_dialog:
            self.test_dialog = testDialogWindow(self)
        self.test_dialog.show()
        self.test_dialog.activateWindow()  
                  
    def display_message(self, txt):
        """Display message in status bar.

        :param str txt: message to display
        """
        self.statusBar.showMessage(txt)
        log.debug(txt)

    def dock_handler(self, status, name):
        """Update dock visibility status"""
        self.dock_status[name] = status

    def update_gui(self):
        """Wrapper for updating parts of GUI when cursor changes position.
        """
        if not self.textEdit:
            return
        current_cursor = self.textEdit.textCursor()
        if current_cursor.block().userData():
            self.text_to_stroke_move()
            self.refresh_steno_display(current_cursor)
            self.display_block_data()
            self.update_style_display(self.textEdit.textCursor().block().userData()["style"])
            self.update_navigation()    

    def update_tape(self, txt):
        """Update tape with new stroke(s).
        :param str txt: line(s) from tape
        """
        # todo: if ever tape format changes, alter here
        lines = txt.splitlines()
        for line in lines:
            self.strokeList.appendPlainText(line)

    def display_block_steno(self, strokes):
        """Update reveal steno dock with element data.

        :param strokes: ``element_collection``
        """
        # clear of last block data
        self.parSteno.clear()
        for ind, el in enumerate(strokes):
            item = QListWidgetItem()
            item.setText(el.to_text())
            item.setData(Qt.ToolTipRole, el.to_display())
            item.setData(Qt.UserRole, ind)
            self.parSteno.addItem(item)     

    def display_block_data(self):
        """Display paragraph attributes and set style if necessary.
        """
        current_cursor = self.textEdit.textCursor()
        block_number = current_cursor.blockNumber()
        block_data = current_cursor.block().userData()
        log.debug(f"Update GUI to display block {block_number} data")
        if not block_data:
            return
        self.editorParagraphLabel.setText(str(block_number))
        if block_data["creationtime"]:
            self.editorCreationTime.setDateTime(QDateTime.fromString(block_data["creationtime"],  "yyyy-MM-ddTHH:mm:ss.zzz")) 
        if block_data["edittime"]:
            self.editorEditTime.setDateTime(QDateTime.fromString(block_data["edittime"],  "yyyy-MM-ddTHH:mm:ss.zzz"))
        if block_data["audiostarttime"]:
            self.editorAudioStart.setTime(QTime.fromString(block_data["audiostarttime"], "HH:mm:ss.zzz"))
        else:
            self.editorAudioStart.setTime(QTime(0, 0, 0, 0))
        if block_data["audioendtime"]:
            self.editorAudioEnd.setTime(QTime.fromString(block_data["audioendtime"], "HH:mm:ss.zzz"))
        else:
            self.editorAudioEnd.setTime(QTime(0, 0, 0, 0))
        if block_data["notes"]:
            self.editorNotes.setText(block_data["notes"])
        else:
            self.editorNotes.clear()
        if not block_data["style"]:
            self.textEdit.to_next_style()
        self.style_selector.setCurrentText(block_data["style"])
        self.textEdit.showPossibilities()

    def refresh_steno_display(self, cursor = None):
        """Refresh steno display with data from cursor paragraph.

        :param cursor: a ``QTextCursor``, default ``None`` for current cursor
        """
        if not self.textEdit:
            return
        if not self.dock_status["dockStenoData"]:
            return
        if not cursor:
            cursor = self.textEdit.textCursor()
        block_strokes = cursor.block().userData()["strokes"]
        self.display_block_steno(block_strokes)

    def refresh_editor_styles(self):
        """Reapply styles to every paragraph.
        """
        if self.textEdit.document().blockCount() > 1000:
            user_choice = QMessageBox.question(self, "Plover2CAT", f"There are {self.textEdit.document().blockCount()} paragraphs. Style refreshing may take some time. Continue?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if user_choice == QMessageBox.No:
                return
        self.textEdit.gen_style_formats()
        # self.progressBar = QProgressBar(self)
        # self.progressBar.setMaximum(self.textEdit.document().blockCount())
        # self.progressBar.setFormat("Re-style paragraph %v")
        # self.statusBar.addWidget(self.progressBar)
        # self.progressBar.show()
        self.mainTabs.hide()
        self.textEdit.setUpdatesEnabled(False)
        self.textEdit.document().blockSignals(True)
        self.textEdit.blockSignals(True)
        # todo: make tempfile, use with, then autosave to tempfile, then reload from tempfile with load_transcript
        with TemporaryDirectory() as f:
            temp_path = pathlib.Path(f, "temp.transcript")
            self.textEdit.save_transcript(temp_path)
            self.textEdit.load_transcript(temp_path)
        self.textEdit.setUpdatesEnabled(True)
        self.textEdit.document().blockSignals(False)
        self.textEdit.blockSignals(False)
        self.statusBar.removeWidget(self.progressBar)
        self.mainTabs.show()

    def update_style_display(self, style):
        """Update GUI display for style properties.

        :param str style: name of style
        """
        # log.debug(f"Updating style GUI for style {style}.")
        block_style = self.textEdit.par_formats[style]
        text_style = self.textEdit.txt_formats[style]
        self.blockFont.setCurrentFont(text_style.font())
        self.blockFontSize.setValue(int(text_style.fontPointSize()))
        self.blockFontBold.setChecked(text_style.font().bold())
        self.blockFontItalic.setChecked(text_style.font().italic())
        self.blockFontUnderline.setChecked(text_style.font().underline())
        self.blockAlignment.setExclusive(False)
        for but in self.blockAlignment.buttons():
            but.setChecked(False)
        if block_style.alignment() == Qt.AlignJustify:
            self.blockJustify.setChecked(True)
        elif block_style.alignment() == Qt.AlignRight:
            self.blockRightAlign.setChecked(True)
        elif block_style.alignment() == Qt.AlignHCenter:
            self.blockCenterAlign.setChecked(True)
        else:
            self.blockLeftAlign.setChecked(True)
        self.blockAlignment.setExclusive(True)
        tabs = block_style.tabPositions()
        if len(tabs) > 1:
            self.blockTabStop.setEnabled(False)
            self.blockTabStop.setValue(0)
            tabs_in = [str(pixel_to_in(t.position)) for t in tabs]
            self.blockTabStop.setSpecialValueText(",".join(tabs_in))
        elif len(tabs) == 1:
            self.blockTabStop.setEnabled(True)
            self.blockTabStop.setSpecialValueText("")
            self.blockTabStop.setValue(pixel_to_in(tabs[0].position))
        else:
            self.blockTabStop.setEnabled(True)
            self.blockTabStop.setSpecialValueText("")
            self.blockTabStop.setValue(0)
        text_indent = block_style.textIndent() if block_style.textIndent() else 0
        left_margin = block_style.leftMargin() if block_style.leftMargin() else 0
        right_margin = block_style.rightMargin() if block_style.rightMargin() else 0
        top_margin = block_style.topMargin() if block_style.topMargin() else 0
        bottom_margin = block_style.bottomMargin() if block_style.bottomMargin() else 0
        line_spacing = int(block_style.lineHeight() if block_style.lineHeight() else 100)
        self.blockTextIndent.setValue(pixel_to_in(text_indent))
        self.blockLeftMargin.setValue(pixel_to_in(left_margin))
        self.blockRightMargin.setValue(pixel_to_in(right_margin))
        self.blockTopMargin.setValue(pixel_to_in(top_margin))
        self.blockBottomMargin.setValue(pixel_to_in(bottom_margin))
        self.blockLineSpace.setValue(line_spacing)
        self.blockParentStyle.clear()
        self.blockParentStyle.addItems([*self.textEdit.styles])
        if "defaultoutlinelevel" in self.textEdit.styles[style]:
            self.blockHeadingLevel.setCurrentText(self.textEdit.styles[style]["defaultoutlinelevel"])
        else:
            self.blockHeadingLevel.setCurrentIndex(0)
        if "parentstylename" in self.textEdit.styles[style]:
            self.blockParentStyle.setCurrentText(self.textEdit.styles[style]["parentstylename"])
        else:
            self.blockParentStyle.setCurrentIndex(-1)
        self.blockNextStyle.clear()
        self.blockNextStyle.addItems([*self.textEdit.styles])
        if "nextstylename" in self.textEdit.styles[style]:
            self.blockNextStyle.setCurrentText(self.textEdit.styles[style]["nextstylename"])
        else:
            self.blockNextStyle.setCurrentIndex(-1)

    def context_menu(self, pos):
        """Create right-click menu at cursor click position.

        :param pos: position to display menu
        """
        log.debug("User activated context menu.")
        menu = QMenu()
        menu.addAction(self.actionRetroactiveDefine)
        menu.addAction(self.actionMergeParagraphs)
        menu.addAction(self.actionSplitParagraph)
        menu.addAction(self.actionCut)
        menu.addAction(self.actionCopy)
        menu.addAction(self.actionPaste)
        menu.exec_(self.textEdit.viewport().mapToGlobal(pos))

    def menu_enabling(self, value = True):
        """Disable (or enable) GUI options for transcript.

        :param bool value: default ``True`` to disable all menus not needed when no transcript open
        """
        log.debug("Menu (dis/en)abling.")
        self.menuEdit.setEnabled(not value)
        self.menuSteno_Actions.setEnabled(not value)
        self.menuInsert.setEnabled(not value)
        self.menuAudio.setEnabled(not value)
        self.menuStyling.setEnabled(not value)
        self.menuDictionary.setEnabled(not value)
        self.menuTools.setEnabled(not value)
        self.menuExport_as.setEnabled(not value)
        self.toolbarSteno.setEnabled(not value)
        self.toolbarAudio.setEnabled(not value)
        self.toolbarEdit.setEnabled(not value)
        self.actionImportRTF.setEnabled(not value)
        self.actionSave.setEnabled(not value)
        self.actionSaveAs.setEnabled(not value)
        self.actionOpenTranscriptFolder.setEnabled(not value)
        self.actionClose.setEnabled(not value)

    def audio_menu_enabling(self, value = True):
        """Disable (or enable) GUI options for when audio is loaded and make signal/slot connections.

        :param bool value: default ``True`` for when audio is present and GUI should change
        """
        self.actionOpenAudio.setEnabled(not value)
        self.actionPlayPause.setEnabled(value)
        self.actionStopAudio.setEnabled(value)
        self.actionSkipForward.setEnabled(value)
        self.actionSkipBack.setEnabled(value)
        self.actionSpeedUp.setEnabled(value)
        self.actionSlowDown.setEnabled(value)
        self.playRate.setEnabled(value)
        self.audioDelay.setEnabled(value)
        self.audio_seeker.setEnabled(value)
        if not self.textEdit:
            return
        if value:
            label_text = "Audio:" + str(self.textEdit.audio_file.name)
            self.audio_label.setText(label_text)
            self.actionPlayPause.triggered.connect(self.textEdit.play_pause_audio)
            self.actionStopAudio.triggered.connect(self.textEdit.player.stop)
            self.update_seeker_track(self.textEdit.audio_position)
            self.update_duration(self.textEdit.player.duration())
            self.audio_seeker.sliderMoved.connect(self.textEdit.player.setPosition)
            self.textEdit.audio_position_changed.connect(self.update_seeker_track)
            self.textEdit.audio_length_changed.connect(self.update_duration)
            self.actionSkipForward.triggered.connect(lambda: self.textEdit.player.setPosition(self.textEdit.player.position() + 5000))
            self.actionSkipBack.triggered.connect(lambda: self.textEdit.player.setPosition(self.textEdit.player.position() - 5000))
            self.playRate.valueChanged.connect(self.textEdit.player.setPlaybackRate)
            self.audioDelay.setValue(self.textEdit.audio_delay)
            self.audioDelay.valueChanged.connect(self.set_audio_delay)
        else:
            self.audio_label.setText("Select file to play audio")
            self.actionPlayPause.triggered.disconnect()
            self.actionStopAudio.triggered.disconnect()
            self.audio_seeker.sliderMoved.disconnect()
            self.actionSkipForward.triggered.disconnect()
            self.actionSkipBack.triggered.disconnect()
            self.playRate.valueChanged.disconnect()
            self.playRate.setValue(1)
            self.update_duration(0)
            self.update_seeker_track(0)
            self.audioDelay.valueChanged.disconnect()
            self.audioDelay.setValue(0)

    def update_duration(self, duration):
        """Update duration label in GUI with duration of media.

        :param int duration: length in milliseconds
        """
        self.audio_seeker.setMaximum(duration)
        self.audio_duration.setText(ms_to_hours(duration))

    def update_seeker_track(self, position):
        """Update position on seeker and label to position of media.

        :param int position: present position of media
        """
        self.audio_seeker.setValue(position)
        self.audio_curr_pos.setText(ms_to_hours(position))

    def get_tapey_tape(self):
        """Obtain suggestions from Tapey Tape file.
        """
        ## from tapeytape default, maybe make selectable in future?
        config_dir = pathlib.Path(plover.oslayer.config.CONFIG_DIR)
        tapey_tape_location = config_dir.joinpath('tapey_tape.txt')
        log.debug("Trying to load tapey tape from default location.")
        if not tapey_tape_location.exists():
            return
        stroke_search = [re.findall(re_strokes,line) for line in open(tapey_tape_location)]
        stroke_search = [x[0] for x in stroke_search if x]
        ## number maybe adjusted in future? both number of occurrences and number of words to place into table
        ## this uses frequency order
        if self.suggest_sort.isChecked():
            most_common_strokes = [word for word, word_count in Counter(stroke_search).items() if word_count > 2]
            most_common_strokes = most_common_strokes[:min(11, len(most_common_strokes) + 1)]
            most_common_strokes = most_common_strokes[::-1]
        else: 
            most_common_strokes= [word for word, word_count in Counter(stroke_search).most_common(10) if word_count > 2]
        first_stroke = [stroke.split()[0] for stroke in most_common_strokes]
        words = [self.engine.lookup(tuple(stroke.split("/"))) for stroke in first_stroke]
        self.suggestTable.clearContents()
        self.suggestTable.setRowCount(len(words))
        self.suggestTable.setColumnCount(2)
        for row in range(len(words)):
            self.suggestTable.setItem(row, 0, QTableWidgetItem(words[row]))
            self.suggestTable.setItem(row, 1, QTableWidgetItem(most_common_strokes[row]))
        self.suggestTable.resizeColumnsToContents()

    def get_clippy(self):
        """Obtain suggestions from plover-clippy-2 file by parsing 
        default clippy output based on color codes.
        """
        config_dir = pathlib.Path(plover.oslayer.config.CONFIG_DIR)
        clippy_location = config_dir.joinpath('clippy_2.org')
        log.debug("Trying to load clippy from default location")
        if not clippy_location.exists():
            # log.debug("Clippy load failed")
            return
        raw_lines = [line for line in open(clippy_location)]
        stroke_search = []
        for line in raw_lines:
            search_hit = clippy_strokes.search(line)
            if search_hit:
                stroke_search.append(search_hit.group(1).split(", "))
        first_stroke_search = [x[0] for x in stroke_search]
        combined_stroke_search = dict(zip(first_stroke_search, stroke_search))
        log.debug("stroke_search = " + str(stroke_search))
        if self.suggest_sort.isChecked():
            most_common_strokes = [word for word, word_count in reversed(Counter(first_stroke_search).items()) if word_count > 2]
            most_common_strokes = most_common_strokes[:min(11, len(most_common_strokes) + 1)]
        else:
            most_common_strokes = [word for word, word_count in Counter(first_stroke_search).most_common(10) if word_count > 2]
        log.debug("most_common_strokes = " + str(most_common_strokes))
        words = [self.engine.lookup(tuple(stroke.split("/"))).strip() for stroke in most_common_strokes]
        log.debug("words = " + str(words))
        self.suggestTable.clearContents()
        self.suggestTable.setRowCount(len(words))
        self.suggestTable.setColumnCount(2)
        for row in range(len(words)):
            self.suggestTable.setItem(row, 0, QTableWidgetItem(words[row]))
            self.suggestTable.setItem(row, 1, QTableWidgetItem(", ".join(combined_stroke_search[most_common_strokes[row]])))
        self.suggestTable.resizeColumnsToContents()

    def get_suggestions(self):
        """Wrapper to get suggestions from Tapey Tape or plover-clippy-2
        """
        if not self.textEdit:
            return
        if not self.dock_status["dockSuggest"]:
            return
        if self.suggest_source.currentText() == "tapey-tape":
            self.get_tapey_tape()
        elif self.suggest_source.currentText() == "clippy_2":
            self.get_clippy()
        else:
            log.debug("Unknown suggestion source %s!" % self.suggest_source.currentText())

    def update_record_time(self):
        """Display recording time in status bar.
        """
        self.display_message(f"Recorded {ms_to_hours(self.textEdit.recorder.duration())}")

    def open_root(self):
        """Open root directory of current transcript.
        """
        selected_folder = pathlib.Path(self.textEdit.file_name)
        self.display_message(f"User open file directory {str(selected_folder)}")
        if platform.startswith("win"):
            startfile(selected_folder)
        elif platform.startswith("linux"):
            subprocess.call(['xdg-open', selected_folder])
        elif platform.startswith("darwin"):
            subprocess.call(['open', selected_folder])
        else:
            self.display_message("Unknown operating system. Not opening file directory.")

    def recent_file_menu(self):
        """Generate menu items for recent files.
        """
        log.debug("Updating recent files menu.")
        self.menuRecentFiles.clear()
        self.clear_layout(self.recentfileflow)
        settings = QSettings("Plover2CAT", "OpenCAT")
        recent_file_paths = settings.value("recentfiles", [])
        for dir_path in recent_file_paths:
            transcript_path = pathlib.Path(dir_path)
            if not transcript_path.exists():
                continue
            label = transcript_path.stem
            action = QAction(label, self.menuRecentFiles)
            action.setData(dir_path)
            action.setToolTip(str(dir_path))
            self.menuRecentFiles.addAction(action)
            tb = QToolButton()
            icon = QtGui.QIcon()
            icon.addFile(":/resources/document-text-large.png", QtCore.QSize(), QtGui.QIcon.Normal, QtGui.QIcon.Off)
            tb.setDefaultAction(action)
            tb.setIcon(icon)
            tb.setIconSize(QSize(32, 32))
            tb.setToolButtonStyle(QtCore.Qt.ToolButtonTextUnderIcon)
            tb.setAutoRaise(True)
            tb.setToolTip(str(dir_path))
            self.recentfileflow.addWidget(tb)

    def clear_layout(self, layout):
        """Clear flow layout for recent files.
        """
        if layout is not None:
            while layout.count():
                item = layout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()
                else:
                    self.clearLayout(item.layout())

    def change_window_font(self):
        """Change window font.
        """
        font, valid = QFontDialog.getFont()
        if valid:
            self.setFont(font)
            log.debug("User set window font.")   

    def change_backgrounds(self):
        """Change window background color.
        """
        palette = self.palette()
        old_color = palette.color(QPalette.Base)
        color = QColorDialog.getColor(old_color)
        if color.isValid():
            palette = self.palette()
            palette.setColor(QPalette.Base, color)
            self.setPalette(palette)
            log.debug("User set background color.")

    def change_tape_font(self):
        """Change paper tape font.
        """
        font, valid = QFontDialog.getFont()
        if valid:
            self.strokeList.setFont(font)
            log.debug("User set paper tape font.")

    def change_highlight_colors(self, action):
        key = action.data()
        el_color = QSettings("Plover2CAT", "OpenCAT").value(key, "black")
        new_color = QColorDialog.getColor(QColor(el_color))
        if new_color.isValid():
            QSettings("Plover2CAT", "OpenCAT").setValue(key, new_color)
        self.update_highlight_color()
        if self.textEdit:
            self.textEdit.get_highlight_colors()
            self.refresh_editor_styles()
    
    def update_highlight_color(self):
        settings = QSettings("Plover2CAT", "OpenCAT")
        el_names = ["stroke", "text", "automatic", "field", "index"]
        self.menuHighlightColors.clear()
        for el in el_names:
            key = f"color{el.title()}"
            el_color = settings.value(key, "black")
            action = QAction(el.title(), self.menuHighlightColors)
            action.setData(key)
            pix = QPixmap(50, 50)
            pix.fill(QColor(el_color))
            ic = QIcon(pix)
            action.setIcon(ic)
            self.menuHighlightColors.addAction(action)
            
    def show_invisible_char(self):
        """Show/hide invisible chars in current transcript.
        """
        doc_options = self.textEdit.document().defaultTextOption()
        if self.actionShowAllCharacters.isChecked():
            self.display_message("User enabled show invisible characters.")      
            doc_options.setFlags(doc_options.flags() | QTextOption.ShowTabsAndSpaces | QTextOption.ShowLineAndParagraphSeparators)
        else:
            self.display_message("User disabled show invisible characters.")      
            doc_options.setFlags(doc_options.flags() & ~QTextOption.ShowTabsAndSpaces & ~QTextOption.ShowLineAndParagraphSeparators)
        self.textEdit.document().setDefaultTextOption(doc_options)

    def calculate_space_width(self, font):
        """Calculate approximate width of a font character in inches and set in GUI.

        :param font: ``QFont``
        """
        new_font = font
        new_font.setPointSize(self.blockFontSize.value())
        metrics = QFontMetrics(new_font)
        space_space = metrics.averageCharWidth()
        self.fontspaceInInch.setValue(round(pixel_to_in(space_space), 2))
        log.debug("Update calculation of chararacter width for selected font.")

    def show_toolbox_pane(self, pane):
        """Show a pane from toolbox if hidden.

        :param pane: a ``QWidget`` in the Toolbox dock's ``QTabWidget``
        """
        if not self.dock_status["dockProp"]:
            self.dockProp.setVisible(True)    
        self.tabWidget.setCurrentWidget(pane)
        log.debug(f"User set {pane.objectName()} pane.") 

    def show_find_replace(self):
        """Show Find/Replace pane in toolbox, and fill search field with text if selection.
        """
        if self.textEdit.textCursor().hasSelection() and self.search_text.isChecked():
            self.search_term.setText(self.textEdit.textCursor().selectedText())
        if not self.dock_status["dockProp"]:
            self.dockProp.setVisible(True)
        self.tabWidget.setCurrentWidget(self.find_replace_pane)
        log.debug("User set find pane visible.")

    def show_stenospell(self):
        """Show Stenospell pane in toolbox, and fill search field with text if selection.
        """
        current_cursor = self.textEdit.textCursor()
        if current_cursor.hasSelection():
            current_block = current_cursor.block()
            start_pos = current_cursor.selectionStart() - current_block.position()
            end_pos = current_cursor.selectionEnd() - current_block.position()
            start_stroke_pos = current_block.userData()["strokes"].stroke_pos_at_pos(start_pos)
            end_stroke_pos = current_block.userData()["strokes"].stroke_pos_at_pos(end_pos)
            underlying_strokes = current_block.userData()["strokes"].extract_steno(start_stroke_pos[0], end_stroke_pos[1])
            underlying_steno = underlying_strokes.to_strokes()
            self.steno_outline.setText(underlying_steno)
        if not self.dock_status["dockProp"]:
            self.dockProp.setVisible(True) 
        self.tabWidget.setCurrentWidget(self.stenospell_pane)  
        log.debug("User set steno spell pane visible.")

    def search_online(self, link):
        """Search online with selected text using link source in default browser.

        Listed sources: Wikipedia, Merriam-Webster, OED, Google, DuckDuckGo.

        :param link: a URL string template for query search
        """
        current_cursor = self.textEdit.textCursor()
        if not current_cursor.hasSelection():
            self.display_message("No text selected for online search.")
            return
        QDesktopServices.openUrl(QUrl(link.format(current_cursor.selectedText())))

    def heading_navigation(self, item):
        """Jump to paragraph from navigation dock.

        :param item: ``QListWidgetItem``, should have par number in data
        """
        block_number = item.data(Qt.UserRole)
        log.debug(f"User navigating to block {block_number}.")
        self.textEdit.navigate_to(block_number)

    def jump_par(self):
        """Jump to paragraph based on user-input paragraph number.
        """
        current_cursor = self.textEdit.textCursor()
        max_blocks = self.textEdit.document().blockCount()
        current_block_num = current_cursor.blockNumber()
        block_num, ok = QInputDialog().getInt(self, "Jump to paragraph...", "Paragraph (0-based): ", current_block_num, 0, max_blocks)
        if ok:
            log.debug(f"User set jump to block {block_num}")
            self.textEdit.navigate_to(block_num)

    def check_undo_stack(self, index):
        """Refresh config and style GUI based on labels in ``QUndoStack``.
        """
        ## bit of hack because it depends on display text of a ``QUndoAction``
        if self.textEdit.undo_stack.undoText().startswith("Config") or self.textEdit.undo_stack.redoText().startswith("Config"):
            self.update_config_gui()
        if self.textEdit.undo_stack.undoText().startswith("Style:") or self.textEdit.undo_stack.redoText().startswith("Style:"):
            self.refresh_editor_styles()
        if self.textEdit.undo_stack.undoText().startswith("Fields:") or self.textEdit.undo_stack.redoText().startswith("Fields:"):
            self.update_config_gui()

    def update_config_gui(self):
        """Update config GUI for parameters in current transcript.
        """
        self.update_field_menu()
        config_contents = self.textEdit.config
        self.style_file_path.setText(pathlib.Path(config_contents["style"]).as_posix())
        self.page_width.setValue(float(config_contents["page_width"]))
        self.page_height.setValue(float(config_contents["page_height"]))
        self.page_left_margin.setValue(float(config_contents["page_left_margin"]))
        self.page_top_margin.setValue(float(config_contents["page_top_margin"]))
        self.page_right_margin.setValue(float(config_contents["page_right_margin"]))
        self.page_bottom_margin.setValue(float(config_contents["page_bottom_margin"]))
        self.enable_line_num.setChecked(config_contents["page_line_numbering"])
        self.line_num_freq.setValue(int(config_contents["page_linenumbering_increment"]))
        self.enable_timestamp.setChecked(config_contents["page_timestamp"])
        self.page_max_char.setValue(int(config_contents["page_max_char"]))
        self.page_max_lines.setValue(int(config_contents["page_max_line"]))
        self.header_left.setText(config_contents["header_left"])
        self.header_center.setText(config_contents["header_center"])
        self.header_right.setText(config_contents["header_right"])
        self.footer_left.setText(config_contents["footer_left"])
        self.footer_center.setText(config_contents["footer_center"])
        self.footer_right.setText(config_contents["footer_right"])
        self.actionAutomaticAffixes.blockSignals(True)
        self.actionAutomaticAffixes.setChecked(config_contents["enable_automatic_affix"])
        self.actionAutomaticAffixes.blockSignals(False)
        self.setup_page()

    def setup_page(self):
        """Set up line wrap and text width for ``QTextDocument`` based on config page size.
        """
        doc = self.textEdit.document()
        width = float(self.textEdit.config["page_width"])
        height = float(self.textEdit.config["page_height"])
        width_pt = int(in_to_pt(width))
        height_pt = int(in_to_pt(height))
        self.textEdit.setLineWrapMode(QTextEdit.FixedPixelWidth)
        self.textEdit.setLineWrapColumnOrWidth(width_pt)
        page_size = QPageSize(QSizeF(width, height), QPageSize.Inch, matchPolicy = QPageSize.FuzzyMatch) 
        doc.setPageSize(page_size.size(QPageSize.Point))

    def update_style_menu(self):
        """Create menu items for each existing style, add shortcuts for first 10.
        """
        # log.debug("Updating style sub-menu.")
        self.menuParagraphStyle.clear()
        for ind, name in enumerate(self.textEdit.styles.keys()):
            label = name
            action = QAction(label, self.menuParagraphStyle)
            if ind < 10:
                action.setShortcut(f"Ctrl+{ind}")
            action.setData(ind)
            self.menuParagraphStyle.addAction(action)

    def update_field_menu(self):
        """Create menu items for each existing field for insertion, add shortcuts for first 10.
        """
        self.display_message("Updating field sub-menu.")
        self.menuField.clear()
        for ind, (k, v) in enumerate(self.textEdit.user_field_dict.items()):
            label = "{%s}: %s" % (k, v)
            action = QAction(label, self.menuField)
            if ind < 10:           
                action.setShortcut("Alt+%d" % ind)
            action.setData(k)
            self.menuField.addAction(action)

    def update_index_menu(self, index_dict = None):
        """Create menu items for index entries for insertion.
        """
        if not index_dict:
            index_dict = self.textEdit.extract_indexes()
        self.display_message("Updating index entry insertion sub-menu.")
        self.menuIndexEntry.clear()
        for ind, (k, v) in enumerate(index_dict.items()):
            label = "%s %s" % (k, v["prefix"])
            action = QAction(label, self.menuIndexEntry)
            action.setObjectName(f"index{ind}")
            action.setData((k, v["prefix"], v["hidden"]))
            self.menuIndexEntry.addAction(action)  

    def update_navigation(self):     
        """Create ``QListWidgetItems`` to display headings in navigation dock.
        """
        self.navigationList.clear()
        if not self.dock_status["dockNavigation"]:
            return
        if not self.textEdit:
            return
        block = self.textEdit.document().begin()
        for i in range(self.textEdit.document().blockCount()):
            block_data = block.userData()
            if not block_data: continue
            if block_data["style"] in self.textEdit.styles and "defaultoutlinelevel" in self.textEdit.styles[block_data["style"]]:
                item = QListWidgetItem()
                level = int(self.textEdit.styles[block_data["style"]]["defaultoutlinelevel"])
                txt = " " * level + block.text()
                item.setText(txt)
                item.setData(Qt.UserRole, block.blockNumber())
                self.navigationList.addItem(item)
            if block == self.textEdit.document().lastBlock():
                break
            block = block.next()   
        log.debug("Nagivation pane updated.")

    def stroke_to_text_move(self):
        """Locate text in transcript based on selected line in tape.
        """
        stroke_cursor = self.strokeList.textCursor()
        edit_cursor = self.textEdit.textCursor()
        self.textEdit.blockSignals(True)
        try:
            stroke_cursor.movePosition(QTextCursor.StartOfBlock)
            stroke_cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
            cursor_position_stroke = stroke_cursor.selectedText().split("|")[2].split(",")
            par = int(cursor_position_stroke[0].replace("(", ""))
            col = int(cursor_position_stroke[1].replace(")", ""))
            edit_cursor.movePosition(QTextCursor.Start)
            for i in range(par):
                edit_cursor.movePosition(QTextCursor.NextBlock)
            for i in range(col):
                edit_cursor.movePosition(QTextCursor.NextCharacter)
            self.textEdit.setTextCursor(edit_cursor)
            log.debug("Move text cursor to tape position.")
        except:
            pass
        self.textEdit.blockSignals(False)

    def text_to_stroke_move(self):
        """Locate stroke line in tape based on cursor position in transcript.
        """
        if not self.dock_status["dockPaper"]:
            return
        if not self.textEdit:
            return
        stroke_cursor = self.strokeList.textCursor()
        edit_cursor = self.textEdit.textCursor()
        edit_block = edit_cursor.block()
        block_data = edit_block.userData()
        self.strokeList.blockSignals(True)
        stroke_text = self.strokeList.document().toPlainText().split("\n")
        pos = edit_cursor.positionInBlock()
        self.cursor_status.setText("Par,Char: {line},{char}".format(line = edit_cursor.blockNumber(), char = pos)) 
        try:
            if edit_cursor.atBlockStart():
                stroke_time = block_data["strokes"].data[0].time
            elif edit_cursor.atBlockEnd():
                stroke_time = block_data["strokes"].data[-1].time
            else:
                stroke_data = block_data["strokes"].extract_steno(pos, pos + 1)
                stroke_time = stroke_data.data[0].time
            # no idea how fast this will be with many many more lines, probably slow
            for index, i in enumerate(stroke_text):
                if i.startswith(stroke_time):
                    stroke_pos = index
                    break
            stroke_cursor.movePosition(QTextCursor.Start)
            for i in range(stroke_pos):
                stroke_cursor.movePosition(QTextCursor.NextBlock)
            stroke_cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
            self.strokeList.setTextCursor(stroke_cursor)
            self.strokeList.setCursorWidth(5)
            self.strokeList.ensureCursorVisible()
        except:
            pass
        self.strokeList.blockSignals(False)

    def update_spell_gui(self):
        """List spellchecking dictionaries in GUI.
        """
        self.dict_selection.clear()
        self.dict_selection.addItem("en_US", "en_US")
        default_spellcheck_path = pathlib.Path(self.textEdit.file_name) / "spellcheck"
        if default_spellcheck_path.exists():
            dics = [file for file in default_spellcheck_path.iterdir() if str(file).endswith("dic")]
            for dic in dics:
                    self.dict_selection.addItem(dic.stem, dic)
        self.dict_selection.setCurrentText(self.textEdit.dictionary_name)

    def clipboard_menu(self):
        """Create menu items of data in storage for pasting.
        """
        self.menuClipboard.clear()
        for ind, snippet in enumerate(self.cutcopy_storage):
            label = snippet.to_text()
            action = QAction(label, self.menuClipboard)
            action.setObjectName(f"clipboard{ind}")
            action.setData(ind)
            self.menuClipboard.addAction(action)

    def display_unsave(self, clean):
        current_tab_index = self.mainTabs.currentIndex()
        if clean:
            self.mainTabs.setTabText(current_tab_index, self.textEdit.file_name.name)
        else:
            self.mainTabs.setTabText(current_tab_index, f"*{self.textEdit.file_name.name}")

    def set_autosave_time(self):
        """Set autosave time interval that applies to all transcripts.
        """
        log.debug("User set autosave time.")
        settings = QSettings("Plover2CAT", "OpenCAT")
        if settings.contains("autosaveinterval"):
            min_save = settings.value("autosaveinterval")
        else:
            min_save = 5      
        num, ok = QInputDialog().getInt(self, "Set autosave interval.", "Minutes:", min_save, 1, 100)
        if ok:
            log.debug(f"User set autosave interval to {num}.")
            settings.setValue("autosaveinterval", num)
            if self.actionEnableAutosave.isChecked():
                self.autosave_setup(True)

    def autosave_setup(self, checked):
        """Set up/stop timer for autosave.
        """
        settings = QSettings("Plover2CAT", "OpenCAT")
        if settings.contains("autosaveinterval"):
            min_save = settings.value("autosaveinterval")
        else:
            min_save = 5
        milli = min_save * 60 * 1000           
        if checked:
            self.autosave_time.stop()
            self.autosave_time.start(milli)
        else:
            self.autosave_time.stop()

    def setup_completion(self, checked):
        """Set/clean up autocompletion for transcript.
        """
        log.debug("Setting up autocompletion.")
        if not checked:
            self.textEdit.setCompleter(None)
            return
        self.completer = QCompleter(self)
        wordlist_path = self.textEdit.file_name / "sources" / "wordlist.json"
        if not wordlist_path.exists():
            log.warning("Wordlist does not exist.")
            QMessageBox.warning(self, "Plover2CAT", "The required file wordlist.json is not available in the sources folder. See user manual for format.")
            self.display_message("Wordlist.json for autocomplete does not exist in sources directory. Passing.")
            return
        check_return = self.engine.reverse_lookup("{#Return}")
        if not check_return:
            log.warning("Active dictionaries missing a {#Return} stroke")
            QMessageBox.warning(self, "Plover2CAT", "No active dictionaries have an outline for {#Return}. One is recommended for autocompletion. See user manual.")
            self.actionAutocompletion.setChecked(False)
            return
        self.completer.setModel(self.model_from_file(str(wordlist_path)))
        self.completer.setModelSorting(QCompleter.CaseInsensitivelySortedModel)
        self.completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.completer.setWrapAround(False)
        self.textEdit.setCompleter(self.completer)
        self.display_message("Autocompletion from wordlist.json enabled.")

    def model_from_file(self, fileName):
        """Load candidates for autocompletion.

        :param fileName: path to JSON file
        """
        f = QFile(fileName)
        if not f.open(QFile.ReadOnly):
            return(QStringListModel(self.completer))
        with open(fileName, "r") as f:
            completer_dict = json.loads(f.read())
        words = QStandardItemModel(self.completer)
        log.debug("Constructing autocomplete model.")
        for key, value in completer_dict.items():
            item = QStandardItem()
            # removes any newlines/tabs, otherwise breaks autocomplete
            key = " ".join(key.split())
            item.setText(key)
            item.setData(value, QtCore.Qt.UserRole)
            words.appendRow(item)
        return(words)

    def create_new(self):
        """Create new transcript.
        """
        transcript_name = "transcript-" + datetime.now().strftime("%Y-%m-%dT%H%M%S")
        transcript_dir = pathlib.Path(gettempdir())
        default_path = transcript_dir / transcript_name
        # newer creation wizard should be here to add additional dictionaries, spellcheck and other data
        # selected_name = QFileDialog.getSaveFileName(self, _("Transcript name and location"), str(default_path))[0]
        # if not selected_name:
        #     return
        self.display_message(f"Creating project files at {str(default_path)}")
        self.open_file(default_path)
        self.textEdit.new_open = True

    def open_file(self, file_path = None):
        """Open transcript.

        :param str file_path: path to transcript directory
        """
        if not file_path:
            name = "Config"
            extension = "config"
            selected_folder = QFileDialog.getOpenFileName( self, _("Open " + name), str(pathlib.Path.home()), _(name + "(*." + extension + ")"))[0]
            if not selected_folder:
                self.display_message("No config file was selected for loading.")
                return
            selected_folder = pathlib.Path(selected_folder).parent
        else:
            selected_folder = pathlib.Path(file_path)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        self.display_message(f"Loading project files from {str(selected_folder)}")   
        editorTab = QtWidgets.QWidget()
        editorTab.setObjectName(f"editorTab_{time.time()}")
        editorLayout = QtWidgets.QHBoxLayout(editorTab)
        editorLayout.setObjectName(f"editorLayout_{time.time()}")
        if self.textEdit:
            self.textEdit.restore_dictionary_from_backup(self.engine)
            self.textEdit.disconnect()
            self.breakdown_connections()
        if self.mainTabs.currentChanged.connect(self.switch_restore):
            self.mainTabs.currentChanged.disconnect()
        self.textEdit = PloverCATEditor(editorTab)
        self.textEdit.load(selected_folder, self.engine)
        sizePolicy = QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Expanding)
        sizePolicy.setHorizontalStretch(3)
        sizePolicy.setVerticalStretch(0)
        sizePolicy.setHeightForWidth(self.textEdit.sizePolicy().hasHeightForWidth())
        self.textEdit.setSizePolicy(sizePolicy) 
        editorLayout.addWidget(self.textEdit)
        tab_index = self.mainTabs.addTab(editorTab, self.textEdit.file_name.name)
        self.mainTabs.setCurrentIndex(tab_index)
        QApplication.restoreOverrideCursor()
        self.recentfile_store(self.textEdit.file_name)
        self.setup_connections()
        self.mainTabs.currentChanged.connect(self.switch_restore)

    def recentfile_open(self, action):
        """Handler to open a recent transcript.
        """
        self.open_file(action.data())

    def recentfile_store(self, path):
        """Store transcript path in settings to open.
        """
        settings = QSettings("Plover2CAT", "OpenCAT")
        recent_file_paths = settings.value("recentfiles", [])
        try:
            recent_file_paths.remove(path)
        except ValueError:
            pass  
        recent_file_paths.insert(0, path)
        deleted = []
        for dir_path in recent_file_paths:
            if not pathlib.Path(dir_path).exists():
                deleted.append(dir_path)
            if pathlib.Path(gettempdir()) in dir_path.parents:
                deleted.append(dir_path)
        for remove_path in deleted:
            recent_file_paths.remove(remove_path)
        del recent_file_paths[10:]
        settings.setValue("recentfiles", recent_file_paths)
        self.recent_file_menu()

    def switch_restore(self, index):
        """Facilitate switching between opened transcripts.

        :param int index: index of tab to switch to
        """
        if not index:
            index = self.mainTabs.currentIndex()
        if index == -1:
            # if no tabs in widget, no closing
            return True        
        if self.textEdit:
            self.textEdit.restore_dictionary_from_backup(self.engine)
            self.breakdown_connections()
            self.textEdit.disconnect()
        focal_transcript = self.mainTabs.widget(index)
        textEdit = focal_transcript.findChild(QTextEdit)
        if textEdit:
            self.textEdit = textEdit
            self.textEdit.load(self.textEdit.file_name, self.engine, load_transcript = False)
            self.setup_connections()
        else:
            self.textEdit = None

    def setup_connections(self):
        """Set up necessary connections between GUI and transcript.
        """
        self.actionUndo = self.textEdit.undo_stack.createUndoAction(self.menuEdit)
        undo_icon = QtGui.QIcon()
        undo_icon.addFile(":/resources/arrow-curve-180.png", QtCore.QSize(), QtGui.QIcon.Normal, QtGui.QIcon.Off)
        self.actionUndo.setIcon(undo_icon)
        self.actionUndo.setShortcutContext(QtCore.Qt.WindowShortcut)
        self.actionUndo.setToolTip("Undo writing or other action")
        self.actionUndo.setShortcut("Ctrl+Z")
        self.actionUndo.setObjectName("actionUndo")
        self.actionRedo = self.textEdit.undo_stack.createRedoAction(self.menuEdit)
        redo_icon = QtGui.QIcon()
        redo_icon.addFile(":/resources/arrow-curve.png", QtCore.QSize(), QtGui.QIcon.Normal, QtGui.QIcon.Off)
        self.actionRedo.setIcon(redo_icon)
        self.actionRedo.setShortcutContext(QtCore.Qt.WindowShortcut)
        self.actionRedo.setToolTip("Redo writing or other action")
        self.actionRedo.setShortcut("Ctrl+Y")
        self.actionRedo.setObjectName("actionRedo")
        if self.textEdit.document().defaultTextOption().flags() and QTextOption.ShowTabsAndSpaces:
            self.actionShowAllCharacters.setChecked(True)
        self.menuEdit.addSeparator()
        self.menuEdit.addAction(self.actionUndo)
        self.menuEdit.addAction(self.actionRedo)
        self.menu_enabling(False)
        # needed to override default undo/redo shortcuts
        self.set_shortcuts()
        self.undoView.setStack(self.textEdit.undo_stack)
        self.update_field_menu()
        self.update_style_menu()
        self.menuParagraphStyle.triggered.connect(lambda action: self.update_paragraph_style(action = action))
        self.style_file_path.setText(str(self.textEdit.config["style"]))
        if pathlib.Path(self.textEdit.config["style"]).suffix == ".json":
            self.style_controls.setEnabled(True)
            self.actionCreateNewStyle.setEnabled(True)
        self.update_index_menu()
        self.update_tape(self.textEdit.tape)
        self.update_spell_gui()
        self.spell_search.clicked.connect(lambda: self.spellcheck())
        self.spell_skip.clicked.connect(lambda: self.spellcheck())
        self.spell_ignore_all.clicked.connect(lambda: self.sp_ignore_all())
        self.spellcheck_suggestions.itemDoubleClicked.connect(self.sp_insert_suggest)
        self.dict_selection.activated.connect(self.set_sp_dict)
        self.update_config_gui()
        self.page_width.valueChanged.connect(lambda value, key = "page_width": self.textEdit.set_config_value(key, value))
        self.page_height.valueChanged.connect(lambda value, key = "page_height": self.textEdit.set_config_value(key, value))
        self.page_left_margin.valueChanged.connect(lambda value, key = "page_left_margin": self.textEdit.set_config_value(key, value))
        self.page_top_margin.valueChanged.connect(lambda value, key = "page_top_margin": self.textEdit.set_config_value(key, value))
        self.page_right_margin.valueChanged.connect(lambda value, key = "page_right_margin": self.textEdit.set_config_value(key, value))
        self.page_bottom_margin.valueChanged.connect(lambda value, key = "page_bottom_margin": self.textEdit.set_config_value(key, value))
        self.enable_line_num.stateChanged.connect(lambda value, key = "page_line_numbering": self.textEdit.set_config_value(key, True if value else False))
        self.line_num_freq.valueChanged.connect(lambda value, key = "page_linenumbering_increment": self.textEdit.set_config_value(key, value))
        self.enable_timestamp.stateChanged.connect(lambda value, key = "page_timestamp": self.textEdit.set_config_value(key, True if value else False))
        self.page_max_char.valueChanged.connect(lambda value, key = "page_max_char": self.textEdit.set_config_value(key, value))
        self.page_max_lines.valueChanged.connect(lambda value, key = "page_max_line": self.textEdit.set_config_value(key, value))
        self.header_left.editingFinished.connect(lambda value, key = "header_left": self.textEdit.set_config_value(key, value))
        self.header_center.editingFinished.connect(lambda value, key = "header_center": self.textEdit.set_config_value(key, value))
        self.header_right.editingFinished.connect(lambda value, key = "header_right": self.textEdit.set_config_value(key, value))
        self.footer_left.editingFinished.connect(lambda value, key = "footer_left": self.textEdit.set_config_value(key, value))
        self.footer_center.editingFinished.connect(lambda value, key = "footer_center": self.textEdit.set_config_value(key, value))
        self.footer_right.editingFinished.connect(lambda value, key = "footer_right": self.textEdit.set_config_value(key, value))
        self.style_selector.clear()
        self.style_selector.addItems([*self.textEdit.styles])
        self.style_selector.activated.connect(self.update_paragraph_style)
        self.submitEdited.setEnabled(True)
        self.submitEdited.clicked.connect(self.edit_paragraph_properties)
        self.search_forward.clicked.connect(lambda: self.search())
        self.search_backward.clicked.connect(lambda: self.search(-1))
        self.find_all.clicked.connect(lambda: self.search_all())
        self.replace_selected.clicked.connect(lambda: self.replace())
        self.replace_all.clicked.connect(lambda: self.replace_everything())        
        self.textEdit.undo_stack.indexChanged.connect(self.check_undo_stack)
        self.textEdit.undo_stack.cleanChanged.connect(self.display_unsave)
        self.textEdit.customContextMenuRequested.connect(self.context_menu)
        self.textEdit.send_message.connect(self.display_message)
        self.textEdit.send_tape.connect(self.update_tape)
        self.textEdit.document().blockCountChanged.connect(lambda: self.get_suggestions())
        self.textEdit.cursorPositionChanged.connect(self.update_gui)
        self.textEdit.player.hasVideoChanged.connect(self.set_up_video)

        # Qt 6 no longer provides isAudioAvailable()/isVideoAvailable().
        # Use presence of an attached audio file and hasVideo() instead.
        if getattr(self.textEdit, "audio_file", None):
            self.audio_menu_enabling()

        if self.textEdit.player.hasVideo():
            self.set_up_video()

    def breakdown_connections(self):
        """Disconnect GUI and transcript.
        """
        if self.actionUndo:
            self.menuEdit.removeAction(self.actionUndo)
            self.actionUndo.deleteLater()
            self.actionUndo = None
        if self.actionRedo:
            self.menuEdit.removeAction(self.actionRedo)
            self.actionRedo.deleteLater()
            self.actionRedo = None
        self.actionShowAllCharacters.setChecked(False)
        if self.cap_worker:
            self.setup_captions(False)
        self.actionCaptioning.setChecked(False)
        self.menu_enabling()
        self.strokeList.clear()
        self.undoView.setStack(None)
        self.dict_selection.clear()
        self.spell_search.clicked.disconnect()
        self.spell_skip.clicked.disconnect()
        self.spellcheck_suggestions.itemDoubleClicked.disconnect()
        self.dict_selection.activated.disconnect()
        self.spell_ignore_all.clicked.disconnect()
        # disconnect all config
        self.page_width.valueChanged.disconnect()
        self.page_height.valueChanged.disconnect()
        self.page_left_margin.valueChanged.disconnect()
        self.page_top_margin.valueChanged.disconnect()
        self.page_right_margin.valueChanged.disconnect()
        self.page_bottom_margin.valueChanged.disconnect()
        self.enable_line_num.stateChanged.disconnect()
        self.line_num_freq.valueChanged.disconnect()
        self.enable_timestamp.stateChanged.disconnect()
        self.page_max_char.valueChanged.disconnect()
        self.page_max_lines.valueChanged.disconnect()
        self.header_left.editingFinished.disconnect()
        self.header_center.editingFinished.disconnect()
        self.header_right.editingFinished.disconnect()
        self.footer_left.editingFinished.disconnect()
        self.footer_center.editingFinished.disconnect()
        self.footer_right.editingFinished.disconnect()
        self.style_selector.clear()
        self.style_selector.activated.disconnect()
        self.textEdit.undo_stack.indexChanged.disconnect(self.check_undo_stack)
        self.textEdit.undo_stack.cleanChanged.disconnect()
        self.textEdit.document().blockCountChanged.disconnect()
        self.submitEdited.clicked.disconnect()
        self.submitEdited.setEnabled(False)
        self.search_forward.clicked.disconnect()
        self.find_all.clicked.disconnect()
        self.replace_selected.clicked.disconnect()
        self.replace_all.clicked.disconnect()
        self.menuParagraphStyle.clear()
        self.menuParagraphStyle.triggered.disconnect()
        self.style_file_path.setText("")
        self.style_controls.setEnabled(False)
        self.actionCreateNewStyle.setEnabled(False)
        self.menuField.clear() # clear field submenu
        self.menuIndexEntry.clear() # clear index submenu
        self.parSteno.clear()
        if getattr(self.textEdit, "audio_file", None):
            self.textEdit.player.stop()
            self.audio_menu_enabling(False)
        if self.video:
            self.videoLayout.removeWidget(self.video)
            self.video.deleteLater()
        
    def action_close(self):
        """Close window after saving settings and checking for multiple transcripts.
        """
        self.display_message("User selected quit.")
        settings = QSettings("Plover2CAT", "OpenCAT")
        settings.setValue("geometry", self.saveGeometry())
        settings.setValue("windowstate", self.saveState())
        settings.setValue("windowfont", self.font().toString())
        settings.setValue("tapefont", self.strokeList.font().toString())
        settings.setValue("backgroundcolor", self.palette().color(QPalette.Base))
        settings.setValue("suggestionsource", int(self.suggest_source.currentIndex()))
        self.display_message("Saved window settings")
        textEdit = self.mainTabs.findChildren(QTextEdit)
        if len(textEdit) > 1:
            QMessageBox.information(self, "Plover2CAT", "Multiple transcripts open. Close each in order to exit.")
            return
        else:
            self.close_file()
        self.display_message("Closing window.")
        self.parent().close()

    def close_file(self, tab_index = None):
        """Close transcript tab, checking for changes.

        :param int tab_index: index of tab to close
        """
        if not tab_index:
            tab_index = self.mainTabs.currentIndex()
        if tab_index == -1:
            # if no tabs in widget, no closing
            return True
        tab_page = self.mainTabs.widget(tab_index)
        if tab_page.objectName().startswith("editorTab"):
            choice = self.textEdit.close_transcript()
            if choice and self.textEdit:
                self.textEdit.disconnect()
                self.breakdown_connections()
                self.textEdit = None
            elif not choice:
                return False
        self.mainTabs.removeTab(tab_index)
        tab_page.deleteLater()
        return True

    def save_file(self):
        """Save current transcript.
        """
        if self.textEdit.new_open:
            self.save_as_file()
        else:
            self.textEdit.save()

    def save_as_file(self):
        """Save current transcript in new location.
        """
        transcript_name = "transcript-" + datetime.now().strftime("%Y-%m-%dT%H%M%S")
        transcript_dir = pathlib.Path.home()
        default_path = transcript_dir / transcript_name
        # newer creation wizard should be here to add additional dictionaries, spellcheck and other data
        selected_name = QFileDialog.getSaveFileName(self, _("Transcript name and location"), str(default_path))[0]
        if not selected_name:
            return
        self.display_message(f"Creating project files at {str(selected_name)}")
        self.textEdit.save_as(selected_name)
        self.textEdit.undo_stack.setClean()
        success = self.close_file()
        if success:
            self.open_file(str(selected_name))

    def autosave(self):
        """Autosave current transcript.
        """
        if not self.textEdit:
            return
        self.textEdit.autosave()

    def revert_file(self):
        """Revert transcript to previous commit.
        """
        if not self.textEdit.repo:
            return
        if not self.textEdit.undo_stack.isClean():
            user_choice = QMessageBox.critical(self, "Revert transcript", "All unsaved changes will be destroyed upon reversion. Session history will be erased. Do you wish to continue?")
            if user_choice == QMessageBox.No:
                return
        commit_choices = self.textEdit.get_dulwich_commits()
        commit_times = [commit_time for commit_id, commit_time in commit_choices]
        commit_time, ok = QInputDialog.getItem(self, "Revert transcript", "Commit", commit_times, 0, False)
        if ok:
            ind = commit_times.index(commit_time)
            commit_id = commit_choices[ind][0]
            self.textEdit.revert_transcript(commit_id)

    def import_rtf(self):
        """Import RTF/CRE transcript file.

        This will erase existing transcript data if to proceed.
        Transcript will close and re-open.
        """
        selected_folder = pathlib.Path(self.textEdit.file_name)
        selected_file = QFileDialog.getOpenFileName(
            self,
            _("Import Transcript"),
            str(selected_folder.joinpath(selected_folder.stem).with_suffix(".rtf"))
            , _("RTF (*.rtf)")
        )
        if not selected_file[0]:
            return
        log.debug(f"Import RTF {selected_file[0]}.")            
        if not self.textEdit.document().isEmpty():
            user_choice = QMessageBox.question(self, "Plover2CAT", "Are you sure you want to import? This erases the present transcript.", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if user_choice == QMessageBox.Yes:
                log.debug("User choice to import and erase present document.")
                pass
            else:
                log.debug("Abort import.")
                return
        self.textEdit.clear()
        self.statusBar.showMessage("Parsing RTF.")
        self.progressBar = QProgressBar(self)
        self.statusBar.addWidget(self.progressBar)
        # self.progressBar.show()
        parse_results = rtf_steno(selected_file[0], self.progressBar)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        parse_results.parse_document()
        QApplication.restoreOverrideCursor()
        style_dict, renamed_indiv_style = load_rtf_styles(parse_results)
        rtf_paragraphs = parse_results.paragraphs
        for ind, par in rtf_paragraphs.items():
            par["style"] = renamed_indiv_style[int(ind)]
        file_path = pathlib.Path(pathlib.Path(selected_file[0]).name).with_suffix(".transcript")
        transcript_dir = self.textEdit.file_name
        new_file_path = transcript_dir.joinpath(transcript_dir.stem).with_suffix(".transcript")
        save_json(rtf_paragraphs, new_file_path)
        style_file_path = self.textEdit.file_name / "styles" / pathlib.Path(pathlib.Path(selected_file[0]).name).with_suffix(".json")
        save_json(remove_empty_from_dict(style_dict), style_file_path)
        self.textEdit.set_config_value("style", str(style_file_path))
        if "paperw" in parse_results.page:
            self.textEdit.set_config_value("page_width", parse_results.page["paperw"])
        if "paperh" in parse_results.page:
            self.textEdit.set_config_value("page_height", parse_results.page["paperh"])
        if "margl" in parse_results.page:
            self.textEdit.set_config_value("page_left_margin", parse_results.page["margl"])
        if "margt" in parse_results.page:
            self.textEdit.set_config_value("page_top_margin", parse_results.page["margt"])
        if "margr" in parse_results.page:
            self.textEdit.set_config_value("page_right_margin", parse_results.page["margr"])
        if "margb" in parse_results.page:
            self.textEdit.set_config_value("page_bottom_margin", parse_results.page["margb"])
        self.textEdit.save_config_file()
        self.textEdit.undo_stack.setClean()
        success = self.close_file()
        if success:
            self.open_file(str(transcript_dir))

    def add_dict(self, dict_path):
        """Add transcript dictionary.
        """
        if not dict_path:
            selected_file = QFileDialog.getOpenFileName(
                self,
                _("Select Dictionary"),
                str(self.textEdit.file_name), _("Dict (*.json)"))[0]
            if not selected_file:
                return
            self.display_message(f"Selected dictionary at {str(selected_file)} to add.")
        else:
            selected_file = dict_path
        self.textEdit.add_dict(selected_file)

    def remove_dict(self, dict_path):
        """Remove transcript dictionary.
        """
        if not dict_path:
            dict_dir_path = self.textEdit.file_name / "dict"
            selected_file = QFileDialog.getOpenFileName(
                self,
                _("Select Dictionary to remove"),
                str(dict_dir_path), _("Dict (*.json)"))[0]
            if not selected_file:
                return
        else:
            selected_file = dict_path
        self.textEdit.remove_dict(selected_file)

    def select_style_file(self):
        """Select a style file for the transcript.
        """
        selected_file = QFileDialog.getOpenFileName(
            self,
            _("Select Style JSON or odt"),
            str(self.textEdit.file_name), _("Style (*.json *.odt)"))[0]
        if not selected_file:
            return
        log.debug(f"User selected style file at {selected_file}.")
        self.textEdit.load_check_styles(selected_file)
        self.style_file_path.setText(str(self.textEdit.get_config_value("style")))
        if pathlib.Path(self.textEdit.config["style"]).suffix == ".json":
            self.style_controls.setEnabled(True)
            self.actionCreateNewStyle.setEnabled(True)
        else:
            self.style_controls.setEnabled(False)
            self.actionCreateNewStyle.setEnabled(False)                    
        self.refresh_editor_styles()

    def style_from_template(self):
        """Generate a style file from ODF or RTF/CRE file.

        This does not set the new file as style.
        """
        selected_file = QFileDialog.getOpenFileName(
            self,
            _("Select Style ODT or RTF/CRE file"),
            str(self.textEdit.file_name), _("Style template file (*.odt *.rtf)"))[0]
        if not selected_file:
            return  
        if selected_file.endswith("odt"):
            json_styles = load_odf_styles(selected_file)
            self.statusBar.showMessage("Extracted ODF styles to styles folder.")
        elif selected_file.endswith("rtf"):
            self.statusBar.showMessage("Parsing RTF.")
            self.progressBar = QProgressBar(self)
            self.statusBar.addWidget(self.progressBar)
            parse_results = rtf_steno(selected_file, self.progressBar)
            parse_results.parse_document()
            json_styles, renamed_indiv_style = load_rtf_styles(parse_results)
            self.statusBar.showMessage("Extracted RTF styles to styles folder.")
        style_file_path = self.textEdit.file_name / "styles" / pathlib.Path(pathlib.Path(selected_file).name).with_suffix(".json")
        save_json(remove_empty_from_dict(json_styles), style_file_path)

    def style_edit(self):
        """Gather attribute values from GUI to set/edit for a style.
        """
        style_name = self.style_selector.currentText()
        log.debug(f"Editing style {style_name}.")
        new_style_dict = {"family": "paragraph"}
        if self.blockParentStyle.currentIndex() != -1:
            # this is important so a style is not based on itself
            if self.blockParentStyle.currentText() != style_name:
                new_style_dict["parentstylename"] = self.blockParentStyle.currentText()
            else:
                QMessageBox.warning(self, "Plover2CAT", "Style cannot be parent of itself.")
                return
        if self.blockNextStyle.currentIndex() != -1:
            new_style_dict["nextstylename"] = self.blockNextStyle.currentText()
        if self.blockHeadingLevel.currentText() != "":
            new_style_dict["defaultoutlinelevel"] = self.blockHeadingLevel.currentText()
        # compare par and text properties to recursive original format
        original_style_par = recursive_style_format(self.textEdit.styles, style_name, prop = "paragraphproperties")
        original_style_txt = recursive_style_format(self.textEdit.styles, style_name, prop = "textproperties")
        log.debug("Old paragraph properties: %s" % original_style_par)
        log.debug("Old text properties: %s" % original_style_txt)
        new_txt_dict = {"fontname": self.blockFont.currentFont().family(), "fontfamily": self.blockFont.currentFont().family(), 
                        "fontsize": "%spt" % self.blockFontSize.value(), "fontweight": "bold" if self.blockFontBold.isChecked() else "", 
                        "fontstyle": "italic" if self.blockFontItalic.isChecked() else "", 
                        "textunderlinetype": "single" if self.blockFontUnderline.isChecked() else "", 
                        "textunderlinestyle": "solid" if self.blockFontUnderline.isChecked() else ""}
        if self.blockJustify.isChecked():
            textalign = "justify"
        elif self.blockRightAlign.isChecked():
            textalign = "right"
        elif self.blockCenterAlign.isChecked():
            textalign = "center"
        else:
            textalign = "left"       
        new_par_dict = {"textalign": textalign, "textindent": "%.2fin" % self.blockTextIndent.value(), 
                        "marginleft": "%.2fin" % self.blockLeftMargin.value(), "marginright": "%.2fin" % self.blockRightMargin.value(), 
                        "margintop": "%.2fin" % self.blockTopMargin.value(), "marginbottom": "%.2fin" % self.blockBottomMargin.value(), 
                        "linespacing": "%d%%" % self.blockLineSpace.value()}
        if self.blockTabStop.isEnabled():
            # the input is disabled with multiple tabstops
            tab_pos = self.blockTabStop.value() if self.blockTabStop.value() != 0 else None
            # do not set if tabstop = 0, weird things might happen
            if tab_pos:
                new_par_dict["tabstop"] = "%.2fin" % tab_pos
        min_txt_style = deepcopy(self.textEdit.get_style_property(style_name, "textproperties"))
        for k, v in new_txt_dict.items():
            if k in original_style_txt and v == original_style_txt[k]:
                continue
            else:
                min_txt_style[k] = v
        min_par_style = deepcopy(self.textEdit.get_style_property(style_name, "paragraphproperties"))
        for k, v in new_par_dict.items():
            if k in original_style_par and v == original_style_par[k]:
                continue
            else:
                min_par_style[k] = v
        new_style_dict["paragraphproperties"] = min_par_style
        new_style_dict["textproperties"] = min_txt_style
        self.textEdit.set_style_properties(style_name, new_style_dict)

    def create_new_style(self):
        """Create new style based on old.
        """
        log.debug("User create new style")
        text, ok = QInputDialog().getText(self, "Create New Style", "Style Name (based on %s)" % self.style_selector.currentText(), inputMethodHints  = Qt.ImhLatinOnly)
        if not ok:
            log.debug("User cancelled style creation")
            return
        log.debug(f"Creating new style with name {text.strip()}")
        if text in self.textEdit.styles:
            QMessageBox.critical(self, "Plover2CAT", "New style cannot have same name as existing style.")
            return
        self.textEdit.set_style_properties(text, {"family": "paragraph", "parentstylename": self.style_selector.currentText()})
        self.style_selector.clear()
        self.style_selector.addItems([*self.textEdit.styles])
        self.update_style_menu()

    def update_paragraph_style(self, index = None, action = None):
        """Set a paragraph's style.

        Can be triggered from style selection dropdown, or style menu.

        :param index: index of selected style from dropdown
        :param action: ``QAction`` containing style name in data
        """
        if not index:
            index = action.data()
        new_style = self.style_selector.itemText(index)
        self.textEdit.set_paragraph_style(new_style)
        self.update_gui()

    def transcript_suggest(self):
        """Open suggestions dialog.
        """
        self.display_message("Generate transcript suggestions.")
        if not self.suggest_dialog:
            self.suggest_dialog = suggestDialogWindow(None, self.engine, scowl)
        self.suggest_dialog.update_text(self.textEdit.toPlainText())
        self.suggest_dialog.show()      
        self.suggest_dialog.activateWindow() 

    def log_to_tape(self, stroke):
        """Log stroke to tape.

        This deals with managing capturing output and window focus.

        :param stroke: stroke to log.
        """
        if not self.textEdit:
            return
        if not self.engine.output and self.engine._machine_params.type == "Keyboard":
            return
        # if window inactive, and not capturing everything, and not enabled, don't do anything
        if not self.textEdit.isActiveWindow() and not self.actionCaptureAllOutput.isChecked():
            return
        self.textEdit.log_to_tape(stroke)             

    def on_send_string(self, string):
        """Set string sent by Plover in transcript.
        """
        self.textEdit.last_string_sent = string

    def count_backspaces(self, backspace):
        """Set number of backspaces sent by Plover in transcript.
        """
        log.debug(f"Plover engine sent {backspace} backspace(s)")
        self.textEdit.last_backspaces_sent = backspace

    def on_stroke(self, stroke_pressed):
        """Send stroke from Plover to transcript.

        This will also trigger displaying captions, managing capturing output and window focus.
        """
        if not self.textEdit:
            return
        if not self.engine.output:
            return
        # do nothing if window not in focus
        if not self.textEdit.isActiveWindow() and not self.actionCaptureAllOutput.isChecked():
            return
        # case if stroke only sends commands
        if not self.textEdit.last_string_sent and self.textEdit.last_backspaces_sent == 0:
            return          
        self.display_captions()
        self.textEdit.on_stroke(stroke_pressed, self.actionCursorAtEnd.isChecked())

    def tape_translate(self):
        """Translate tape contents into transcript.
        """
        if not self.engine.output:
            choice = QMessageBox.warning(self, "Plover2CAT", "Plover is not enabled. Enable output?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if choice == QMessageBox.Yes:
                self.engine.set_output(True)
            else:
                return
        # do not erase any content before, case of too many asterisks for example
        self.engine.clear_translator_state()
        # bit of a hack since triggering stroked hook through code
        selected_file = QFileDialog.getOpenFileName(
            self,
            _("Select tape file to translate"),
            str(self.textEdit.file_name), _("Tape (*.tape *.txt)"))[0]
        if not selected_file:
            return
        transcript_dir = self.textEdit.file_name 
        if pathlib.Path(selected_file) == transcript_dir.joinpath(transcript_dir.stem).with_suffix(".tape"):
            self.statusBar.showMessage("Cannot translate from own transcript tape.")
            return
        selected_file = pathlib.Path(selected_file)   
        paper_format, ok = QInputDialog.getItem(self, "Translate Tape", "Format of tape file:", ["Plover2CAT", "Plover (raw)", "Plover (paper)"], editable = False)
        log.debug(f"Translating tape from {selected_file} with {paper_format} format.")
        self.mainTabs.hide()
        self.textEdit.blockSignals(True)
        self.textEdit.document().blockSignals(True)
        self.kc = self.engine._keyboard_emulation
        self.engine._keyboard_emulation = mock_output()        
        count = 0
        if paper_format == "Plover (raw)":
            with open(selected_file) as f:
                for line in f:
                    stroke = Stroke(normalize_stroke(line.strip().replace(" ", "")))
                    self.engine._translator.translate(stroke)
                    self.engine._trigger_hook('stroked', stroke)
                    count += 1
                    if count > 100:
                        self.textEdit.undo_stack.clear()
                        count = 0
        elif paper_format == "Plover2CAT":
            with open(selected_file) as f:
                for line in f:
                    stroke_contents = line.strip().split("|")[3]
                    keys = []
                    for i in range(len(stroke_contents)):
                        if not stroke_contents[i].isspace() and i < len(plover.system.KEYS):
                            keys.append(plover.system.KEYS[i])                    
                    self.engine._translator.translate(Stroke(keys))
                    self.engine._trigger_hook('stroked', Stroke(keys))
                    count += 1
                    if count > 100:
                        self.textEdit.undo_stack.clear()
                        count = 0
        elif paper_format == "Plover (paper)":
            with open(selected_file) as f:
                for line in f:
                    keys = []
                    for i in range(len(line)):
                        if not line[i].isspace() and i < len(plover.system.KEYS):
                            keys.append(plover.system.KEYS[i])
                    self.engine._translator.translate(Stroke(keys))
                    self.engine._trigger_hook('stroked', Stroke(keys))
                    count += 1
                    if count > 100:
                        self.textEdit.undo_stack.clear()
                        count = 0
        self.textEdit.undo_stack.clear()
        self.textEdit.document().blockSignals(False)
        self.textEdit.blockSignals(False)
        self.mainTabs.show()
        self.engine._keyboard_emulation = self.kc
        # todo, if format has time data, that should be inserted into stroke data of editor too

    def reset_paragraph(self):
        """Remove all data from paragraph block, and erase action history.
        """
        user_choice = QMessageBox.critical(self, "Plover2CAT", "A reset will clear all data from this paragraph. This cannot be undone. You will lose all history. Are you sure?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if user_choice != QMessageBox.Yes:
            return
        log.debug("User trigger paragraph reset.")
        self.textEdit.undo_stack.clear()
        log.debug("History cleared.")
        current_cursor = self.textEdit.textCursor()
        current_block = current_cursor.block()
        current_block.setUserData(BlockUserData())
        current_block.setUserState(1)
        current_cursor.select(QTextCursor.BlockUnderCursor)
        current_cursor.removeSelectedText()

    def cut_steno(self, cut = True):
        """Cut/copy selection and generate menu item.
        """
        res = self.textEdit.cut_steno(cut = cut)
        self.cutcopy_storage.appendleft(res)
        self.clipboard_menu()

    def paste_steno(self, action = None):
        """Paste into transcript.

        Each element in the content is individually inserted,
        as images would not work otherwise.

        :param action: a `QAction` containing data to be pasted
        """
        log.debug("Performing pasting.")
        index = 0
        if action:
            index = action.data()
        store_data = deepcopy(self.cutcopy_storage[index])
        if store_data == "":
            self.display_message("Nothing in clipboard. Cut or copy text to paste.")
            return
        ea = element_actions()
        current_cursor = self.textEdit.textCursor()
        current_block_num = current_cursor.blockNumber()
        # current_block = self.textEdit.document().findBlockByNumber(current_block_num)
        start_pos = current_cursor.selectionStart() - current_block.position()
        self.textEdit.undo_stack.beginMacro(f"Paste: {store_data.to_text()}")
        self.textEdit.blockSignals(True)
        for el in store_data:
            current_block = self.textEdit.textCursor().blockNumber()
            current_pos = self.textEdit.textCursor().positionInBlock()
            cmd = ea.make_action(self.textEdit, current_block_num, current_pos, el)
            self.textEdit.undo_stack.push(cmd)
        self.textEdit.blockSignals(False)
        self.textEdit.undo_stack.endMacro()
        self.display_message(f"Pasting to paragraph {current_block_num} at position {start_pos}.")  

    def define_retroactive(self):
        """Define outline for selected text and update transcript.
        """
        log.debug("Define retroactive.")
        current_cursor = self.textEdit.textCursor()
        current_block = current_cursor.block()
        if not current_cursor.hasSelection():
            log.debug("No text selected, skipping")
            self.statusBar.showMessage("Selection needed for define.")
            return
        start_pos = current_cursor.selectionStart() - current_block.position()
        # end_pos is in prep for future multi-stroke untrans
        end_pos = current_cursor.selectionEnd() - current_block.position()
        start_stroke_pos = current_block.userData()["strokes"].stroke_pos_at_pos(start_pos)
        end_stroke_pos = current_block.userData()["strokes"].stroke_pos_at_pos(end_pos)
        current_cursor.setPosition(current_block.position() + start_stroke_pos[0])
        current_cursor.setPosition(current_block.position() + end_stroke_pos[1], QTextCursor.KeepAnchor)
        self.textEdit.setTextCursor(current_cursor)
        underlying_strokes = current_block.userData()["strokes"].extract_steno(start_stroke_pos[0], end_stroke_pos[1])
        underlying_steno = underlying_strokes.to_strokes()
        selected_untrans = current_cursor.selectedText()
        text, ok = QInputDialog().getText(self, "Retroactive Define", "Stroke: %s \nTranslation:" % underlying_steno)
        if self.textEdit.config["space_placement"] == "Before Output":
            text = " " + text.strip()
        else:
            text = text.strip() + " "
        if ok:
            log.debug(f"Define: Outline {underlying_steno} with translation {text}.")
            self.engine.add_translation(normalize_steno(underlying_steno, strict = True), text.strip())
            hold_replace_text = self.replace_term.text()
            hold_search_text = self.search_term.text()
            hold_search_case = self.search_case.isChecked()
            hold_search_whole = self.search_whole.isChecked()
            self.replace_term.setText(text)
            self.search_term.setText(selected_untrans)
            self.search_case.setChecked(False)
            self.search_whole.setChecked(False)
            self.replace_everything(steno = underlying_steno)
            self.replace_term.setText(hold_replace_text)
            self.search_term.setText(hold_search_text)
            self.search_case.setChecked(hold_search_case)
            self.search_whole.setChecked(hold_search_whole)
            current_cursor = self.textEdit.textCursor()
            current_cursor.movePosition(QTextCursor.End)
            self.textEdit.setTextCursor(current_cursor)

    def define_scan(self):
        """Find last untranslated in transcript and define.
        """
        log.debug("Scan to redefine.")
        search_result = self.untrans_search(-1)
        self.define_retroactive()

    def delete_scan(self):
        """Find last untranslated and remove.
        """
        log.debug("Scan to delete.")
        search_result = self.untrans_search(-1)
        if not search_result:
            return
        self.textEdit.cut_steno(store = False)

    def add_autocomplete_item(self):
        """Add autocomplete term to wordlist.
        """
        log.debug("Add term to autocomplete.")
        current_cursor = self.textEdit.textCursor()
        if not current_cursor.hasSelection():
            self.statusBar.showMessage("No text selected for autocomplete")
            return
        current_block = current_cursor.block()
        selected_text = current_cursor.selectedText()        
        start_pos = current_cursor.selectionStart() - current_block.position()
        # end_pos has a one char deletion since otherwise it will include unwanted next stroke
        end_pos = current_cursor.selectionEnd() - current_block.position() - 1
        start_stroke_pos = current_block.userData()["strokes"].stroke_pos_at_pos(start_pos)
        end_stroke_pos = current_block.userData()["strokes"].stroke_pos_at_pos(end_pos)
        underlying_strokes = current_block.userData()["strokes"].extract_steno(start_stroke_pos[0], end_stroke_pos[1])
        underlying_steno = underlying_strokes.to_strokes()
        text, ok = QInputDialog().getText(self, "Add Autocomplete Term", "Text: %s \nSteno:" % selected_text, text = underlying_steno)
        if not ok:
            return
        wordlist_path = self.textEdit.file_name / "sources" / "wordlist.json"
        if wordlist_path.exists():
            with open(wordlist_path, "r") as f:
                completer_dict = json.loads(f.read())
        else:
            completer_dict = {}
        completer_dict[selected_text.strip()] = text
        save_json(completer_dict, wordlist_path)
        log.debug(f"Adding term {text} to autocompletion.")
        self.setup_completion(self.actionAutocompletion.isChecked())

    def enable_affix(self, check):
        """Toggle paragraph affixes.
        """
        self.display_message("Toggle automatic paragraph affixes.")
        self.textEdit.set_config_value("enable_automatic_affix", check)

    def edit_auto_affixes(self):
        """Open affix editor.
        """
        if not self.auto_paragraph_affixes:
            self.display_message("No pre-existing affix dict.")
        self.affix_dialog = affixDialogWindow(self.auto_paragraph_affixes, [*self.styles])
        res = self.affix_dialog.exec_()
        if res == QDialog.Accepted:
            self.display_message("Updating paragraph affixes.")
            self.auto_paragraph_affixes = self.affix_dialog.affix_dict

    def insert_text(self, text = None):
        """Insert text.

        :param str text: text to insert, if ``None``, opens modal for input
        """
        ## todo: should be removed once output between keyboard and machine is figured out
        if not text:
            text, ok = QInputDialog().getText(self, "Insert Normal Text", "Text to insert")
            if not ok:
                return
        self.display_message(f"Inserting normal text {text}.")
        self.textEdit.insert_text(text)

    def insert_image(self):
        """Insert image.
        """
        selected_file = QFileDialog.getOpenFileName(self, _("Select Image"), str(self.textEdit.file_name), 
                            _("Image Files(*.png *.jpg *jpeg)"))[0]
        if not selected_file:
            self.display_message("No image selected, aborting")
            return
        self.display_message(f"User selected image file: {selected_file}")
        self.textEdit.insert_image(selected_file)

    def insert_field(self, action):
        """Insert field.

        :param action: ``QAction`` with field name data.
        """
        name = action.data()
        self.display_message(f"Insert field {name}.")
        self.textEdit.insert_field(name)

    def edit_fields(self):
        """Open fields editor.
        """
        self.field_dialog = fieldDialogWindow(self.textEdit.user_field_dict)
        res = self.field_dialog.exec_()
        if res == QDialog.Accepted:
            self.textEdit.update_fields(self.field_dialog.user_field_dict)  

    def edit_indices(self):
        """Open index editor.
        """
        if not self.index_dialog:
            self.index_dialog = indexDialogWindow({})
        present_index = self.textEdit.extract_indexes()
        if present_index:
            self.index_dialog.update_dict(present_index)
        self.index_dialog.show()
        self.index_dialog.index_insert.connect(self.insert_index_entry)
        self.index_dialog.updated_dict.connect(self.update_indices)
        self.index_dialog.activateWindow()   

    def insert_index_entry(self, el = None, action = None):
        """Insert index entry.

        One of ``el`` or ``action`` has to be specified.

        :param el: ``index_text`` element
        :param action: ``QAction`` with index entry data.
        """
        current_cursor = self.textEdit.textCursor()
        if el is None:
            index_name = action.data()[0]
            index_prefix = action.data()[1]
            index_hidden = action.data()[2]
            if current_cursor.hasSelection():
                txt = current_cursor.selectedText()
                ok = True
            else:
                txt, ok = QInputDialog.getText(self, f"Quick insert index {index_name}", f"{index_prefix}")
            if not ok:
                return
            el = index_text(prefix = index_prefix, indexname = index_name, hidden = index_hidden, text = txt)
        self.textEdit.insert_index_entry(el)
        if not self.index_dialog:
            self.index_dialog = indexDialogWindow({})
        present_index = self.textEdit.extract_indexes()
        if present_index:
            self.index_dialog.update_dict(present_index)

    def update_indices(self):
        """Have transcript update index entries and regenerate menu.
        """
        present_index = self.textEdit.extract_indexes()
        new_index = self.index_dialog.index_dict
        if not present_index:
            return
        self.textEdit.update_indices(present_index, new_index)
        self.update_index_menu(self.index_dialog.index_dict)

    def edit_paragraph_properties(self):
        """Collect paragraph properties from GUI to set.
        """
        block_number = int(self.editorParagraphLabel.text())
        self.textEdit.undo_stack.beginMacro("Update paragraph properties")
        self.textEdit.set_paragraph_property(block_number, "creationtime", self.editorCreationTime.dateTime().toString(Qt.ISODateWithMs))
        val = self.editorAudioStart.time().toString(Qt.ISODateWithMs)
        if val != "00:00:00.000":
            self.textEdit.set_paragraph_property(block_number, "audiostarttime", val)
        val = self.editorAudioEnd.time().toString(Qt.ISODateWithMs)
        if val != "00:00:00.000":
            self.textEdit.set_paragraph_property(block_number, "audioendtime", val)  
        val = self.editorEditTime.dateTime().toString(Qt.ISODateWithMs)
        if val != "2000-01-01T00:00:00.000":
            self.textEdit.set_paragraph_property(block_number, "edittime", val)
        self.textEdit.set_paragraph_property(block_number, "notes", self.editorNotes.text())
        self.textEdit.undo_stack.endMacro()

    def spell_steno(self):
        """Search and display closest outlines.
        """
        outline = self.steno_outline.text()
        pos = multi_gen_alternative(outline)
        res = get_sorted_suggestions(pos, self.engine)
        self.stenospell_res.clear()
        for candidate in res:
            self.stenospell_res.addItem(candidate[0])

    def set_sp_dict(self, index):
        """Load selected spellchecking dictionary.
        """
        lang = self.dict_selection.itemText(index)
        log.debug("Selecting %s dictionary for spellcheck" % lang)
        dict_path = self.dict_selection.itemData(index)
        self.textEdit.load_spellcheck_dict(dict_path)

    def sp_check(self, word):
        """Perform spellcheck for a word.
        """
        return self.textEdit.dictionary.lookup(word)

    def spellcheck(self):
        """Scan text word by word and spellcheck.
        """
        log.debug("Perform spellcheck.")
        current_cursor = self.textEdit.textCursor()
        old_cursor_position = current_cursor.block().position()
        self.textEdit.setTextCursor(current_cursor)
        while not current_cursor.atEnd():
            current_cursor.movePosition(QTextCursor.NextWord)
            current_cursor.movePosition(QTextCursor.EndOfWord, QTextCursor.KeepAnchor)
            result = self.sp_check(current_cursor.selectedText())
            if not result and current_cursor.selectedText() not in self.textEdit.spell_ignore:
                self.textEdit.setTextCursor(current_cursor)
                log.debug("Spellcheck: this word %s not in dictionary." % current_cursor.selectedText())
                suggestions = [sug for sug in self.textEdit.dictionary.suggest(current_cursor.selectedText())]
                self.spellcheck_result.setText(current_cursor.selectedText())
                self.spellcheck_suggestions.clear()
                self.spellcheck_suggestions.addItems(suggestions)
                break
        if current_cursor.atEnd():
            QMessageBox.information(self, "Plover2CAT", "End of document.")

    def sp_ignore_all(self):
        """Add word to be ignored by spellchecking.
        """
        if self.spellcheck_result.text() != "":
            self.textEdit.spell_ignore.append(self.spellcheck_result.text())
            log.debug("Ignored spellcheck words: %s" % self.textEdit.spell_ignore)
        self.spellcheck()

    def sp_insert_suggest(self, item = None):
        """Perform spellcheck replacement.

        :param item: a ``QListWidgetItem``, if ``None``, use selected from GUI
        """
        if not item:
            item = self.spellcheck_suggestions.currentItem()
        log.debug("Spellcheck correction: %s" % item.text())
        self.textEdit.undo_stack.beginMacro("Spellcheck: correct to %s" % item.text())
        self.replace(to_next = False, steno = "", replace_term= item.text())
        self.textEdit.undo_stack.endMacro()  

    def search(self, direction = 1):
        """Search wrapper.

        :param int direction: 1 to search forward, -1 to search back
        :return: success of search
        """
        if self.search_untrans.isChecked():
            search_status = self.untrans_search(direction)
        elif self.search_steno.isChecked():
            search_status = self.steno_wrapped_search(direction)
        else:
            search_status = self.text_search(direction)
        return(search_status)

    def search_all(self):
        """Find all matches and display in pane.
        """
        cursor = self.textEdit.textCursor()
        old_wrap_state = self.search_wrap.isChecked()
        if old_wrap_state:
            self.search_wrap.setChecked(False)
        old_cursor_position = cursor.block().position()        
        cursor.movePosition(QTextCursor.Start)
        search_status = True
        log.debug("Search all, starting from beginning.")
        while search_status:
            search_status = self.search()
            if search_status is None:
                break
            match_start = self.textEdit.textCursor().selectionStart()
            match_end = self.textEdit.textCursor().selectionEnd()
            item = QListWidgetItem()
            current_cursor = self.textEdit.textCursor()
            current_cursor.movePosition(QTextCursor.PreviousWord, QTextCursor.MoveAnchor, 2)
            while current_cursor.selectionEnd() < match_end:
                current_cursor.movePosition(QTextCursor.NextWord, QTextCursor.KeepAnchor, 2)
            match_text = current_cursor.selectedText()
            item.setText(match_text)
            item.setData(Qt.UserRole, (match_start, match_end))
            self.searchResults.addItem(item)
        log.debug("Attempting to set cursor back to original position after search all.")
        cursor.setPosition(old_cursor_position)
        self.textEdit.setTextCursor(cursor)
        self.search_wrap.setChecked(old_wrap_state)
        # store search options
        # then set wrapped false
        # move cursor to top of document
        # loop search
        # for each match, append existing cursor with selection to qtextedit.extraSelections
        # at end of document, stop
        # for each selection, reverse 2 words
        # add as item to qlistview, elide right 

    def search_navigation(self, item):
        start_pos, end_pos = item.data(Qt.UserRole)
        log.debug("Navigating to selected search match.")
        current_cursor = self.textEdit.textCursor()
        current_cursor.setPosition(start_pos)
        current_cursor.setPosition(end_pos, QTextCursor.KeepAnchor)
        self.textEdit.setTextCursor(current_cursor)

    def text_search(self, direction = 1):
        """Search text.

        Uses the ``QTextDocument`` search functionalities.
        """
        flags = QTextDocument.FindFlags()
        search = self.search_term.text()
        if self.search_case.isChecked():
            flags |= QTextDocument.FindCaseSensitively
        if self.search_whole.isChecked():
            flags |= QTextDocument.FindWholeWords
        if direction == -1:
            flags |= QTextDocument.FindBackward
        cursor = self.textEdit.textCursor()
        log.debug("Performing text search with term %s, flags %s.", search, flags)
        found = self.textEdit.document().find(search, cursor, flags)
        if not found.isNull():
            log.debug("Search success.")
            self.textEdit.setTextCursor(found)
            self.statusBar.showMessage("Match found")
            return True
        elif self.search_wrap.isChecked():
            log.debug("Search failure. Wrapping.")
            if direction == 1:
                cursor.movePosition(QTextCursor.Start)
            else:
                cursor.movePosition(QTextCursor.End)
            found = self.textEdit.document().find(search, cursor, flags)
            if not found.isNull():
                log.debug("Search success.")
                self.textEdit.setTextCursor(found)
                self.statusBar.showMessage("Wrapped search. Match found.")
                return True
            else:
                log.debug("Search failure.")
                self.statusBar.showMessage("Wrapped search. No match found.")
                return None
        else:
            log.debug("Search failure.")
            self.statusBar.showMessage("No match found.")
            return None

    def steno_wrapped_search(self, direction = 1):
        """Wrapper for steno search.

        :return: search status
        """
        log.debug("Steno search.")
        found = self.steno_search(direction = direction)
        if not found and self.search_wrap.isChecked():
            log.debug("Wrap steno search.")
            cursor = self.textEdit.textCursor()
            if direction == -1:
                log.debug("Search starting from end.")
                cursor.movePosition(QTextCursor.End)
            else:
                log.debug("Search starting from top.")
                cursor.movePosition(QTextCursor.Start)
            self.textEdit.setTextCursor(cursor)
            found = self.steno_search(direction = direction)
        return(found)

    def steno_search(self, direction = 1):
        """Search steno underlying each paragraph.
        """
        cursor = self.textEdit.textCursor()
        steno = self.search_term.text()
        log.debug("Searching for stroke %s in stroke data.", steno)
        if direction == -1:
            current_block = cursor.block()
            if cursor.hasSelection():
                start_pos = cursor.selectionStart()
                cursor.setPosition(start_pos)
            cursor.movePosition(QTextCursor.PreviousCharacter, QTextCursor.MoveAnchor)
            self.textEdit.setTextCursor(cursor)
            cursor_pos = cursor.positionInBlock()
            stroke_data = current_block.userData()["strokes"].extract_steno(0, cursor_pos)
            while True:
                check_match = stroke_data.search_strokes(steno)
                if check_match is not None:
                    break
                if current_block == self.textEdit.document().firstBlock():
                    # end search after searching first block
                    check_match = None
                    break
                current_block = current_block.previous()
                stroke_data = current_block.userData()["strokes"] 
            if check_match is not None:
                block_pos = current_block.position()
                cursor.setPosition(block_pos + check_match[0])
                cursor.setPosition(block_pos + check_match[1], QTextCursor.KeepAnchor)
                self.textEdit.setTextCursor(cursor)
                log.debug("Search success.")
                self.statusBar.showMessage("Steno match found.")
                return True
            else:
                log.debug("Search failure.")
                self.statusBar.showMessage("No steno match found.")
                return None                                                                            
        else:
            current_block = cursor.block()
            if cursor.hasSelection():
                start_pos = cursor.selectionEnd()
                cursor.setPosition(start_pos + 1)
            cursor.movePosition(QTextCursor.NextCharacter, QTextCursor.MoveAnchor)
            self.textEdit.setTextCursor(cursor)
            cursor_pos = cursor.positionInBlock()
            stroke_data = current_block.userData()["strokes"].extract_steno(cursor_pos, len(current_block.userData()["strokes"]))
            while True:
                # this is different from loop above since there is an offset 
                # so cursor pos below has to add the offset
                check_match = stroke_data.search_strokes(steno)
                if check_match is not None:
                    break
                if current_block == self.textEdit.document().lastBlock():
                    # end search after searching last block
                    check_match = None
                    break
                current_block = current_block.next()
                stroke_data = current_block.userData()["strokes"] 
                cursor_pos = 0
            if check_match is not None:
                block_pos = current_block.position()               
                cursor.setPosition(block_pos + cursor_pos + check_match[0])
                cursor.setPosition(block_pos + cursor_pos + check_match[1], QTextCursor.KeepAnchor)
                self.textEdit.setTextCursor(cursor)
                log.debug("Search success.")
                self.statusBar.showMessage("Steno match found.")
                return True
            else:
                log.debug("Search failure.")
                self.statusBar.showMessage("No steno match found.")
                return None

    def untrans_search(self, direction = 1):
        """Search for untranslated steno.
        """
        flags = QTextDocument.FindFlags()
        untrans_reg = QRegularExpression("(\\b|\\*)(?=[STKPWHRAO*EUFBLGDZ]{3,})S?T?K?P?W?H?R?A?O?\\*?E?U?F?R?P?B?L?G?T?S?D?Z?\\b")
        if direction == -1:
            flags |= QTextDocument.FindBackward
        cursor = self.textEdit.textCursor()
        found = self.textEdit.document().find(untrans_reg, cursor, flags)
        log.debug("Search for untranslated steno.")
        if not found.isNull():
            self.textEdit.setTextCursor(found)
            log.debug("Search success.")
            self.statusBar.showMessage("Untrans found") 
            return True
        elif self.search_wrap.isChecked():
            if direction == 1:
                cursor.movePosition(QTextCursor.Start)
            else:
                cursor.movePosition(QTextCursor.End)
            found = self.textEdit.document().find(untrans_reg, cursor, flags)
            if not found.isNull():
                self.textEdit.setTextCursor(found)
                log.debug("Wrapped. Search success.")
                self.statusBar.showMessage("Wrapped search. Untrans found.")
                return True
            else:
                log.debug("Wrapped. Search failure.")
                self.statusBar.showMessage("Wrapped search. No untrans found.")
                return None
        else:
            log.debug("Search failure.")
            self.statusBar.showMessage("No untrans found.") 
            return None     

    def search_text_options(self):
        """Enable GUI options for text search.
        """
        if self.search_text.isChecked():
            log.debug("Set options for text search.")
            self.search_case.setEnabled(True)
            self.search_whole.setChecked(False)
            self.search_term.setEnabled(True)
            self.search_whole.setEnabled(True)

    def search_steno_options(self):
        """Enable GUI options for steno search.
        """
        if self.search_steno.isChecked():
            log.debug("Set options for steno search.")
            self.search_case.setEnabled(False)
            self.search_whole.setChecked(True)
            self.search_term.setEnabled(True)
            self.search_whole.setEnabled(False)

    def search_untrans_options(self):
        """Enable GUI options for untranslated steno search.
        """
        if self.search_untrans.isChecked():
            log.debug("Set options for untrans search.")
            self.search_term.setEnabled(False)
            self.search_case.setEnabled(False)
            self.search_whole.setChecked(False)
            self.search_whole.setEnabled(False)  

    def replace(self, to_next = True, steno = "", replace_term = None):
        """Perform replacement.

        :param bool to_next: do search for next match
        :param str steno: steno to put into replacement element
        :param replace_term: text to put as replacement, or text from GUI
        """
        log.debug("Perform replacement.")
        if not replace_term:
            replace_term = self.replace_term.text()
        if self.textEdit.textCursor().hasSelection():
            self.textEdit.replace(steno, replace_term)
        if to_next:
            log.debug("Moving to next match.")        
            if self.search_untrans.isChecked():
                search_status = self.untrans_search()
            elif self.search_steno.isChecked():
                search_status = self.steno_wrapped_search()
            else:
                search_status = self.text_search()
            return(search_status)        

    def replace_everything(self, steno = ""):
        """Replace all matches in transcript.

        :param steno: the steno for the replacement element, default ``""``
        """
        cursor = self.textEdit.textCursor()
        old_wrap_state = self.search_wrap.isChecked()
        if old_wrap_state:
            self.search_wrap.setChecked(False)
        old_cursor_position = cursor.block().position()        
        cursor.movePosition(QTextCursor.Start)
        self.textEdit.setTextCursor(cursor)
        search_status = True
        log.debug("Replace all, starting from beginning.")
        self.textEdit.undo_stack.beginMacro("Replace All")
        while search_status:
            search_status = self.search()
            if search_status is None:
                break
            self.replace(to_next = False, steno = steno)
        self.textEdit.undo_stack.endMacro()
        # not the exact position but hopefully close
        log.debug("Attempting to set cursor back to original position after replacements.")
        cursor.setPosition(old_cursor_position)
        self.textEdit.setTextCursor(cursor)
        self.search_wrap.setChecked(old_wrap_state)

    def open_audio(self):
        """Select media file for playing.
        """
        if self.textEdit.recorder.recorderState() == QMediaRecorder.StoppedState:
            pass
        else:
            QMessageBox.information(self, "Plover2CAT", "Recording in progress. Stop recording before loading media file.")
            return
        audio_file = QFileDialog.getOpenFileName(self, _("Select Media File"), str(self.textEdit.file_name), "Media Files (*.mp3 *.ogg *.wav *.mp4 *.mov *.wmv *.avi)")
        if audio_file[0]:
            self.textEdit.load_audio(audio_file[0])
            self.audio_menu_enabling()

    def set_audio_delay(self, value):
        """Set offset for media.
        """
        self.textEdit.audio_delay = value

    def set_up_video(self, avail):
        """Set up video for media.

        :param bool avail: whether media has video or not
        """
        if avail:
            self.display_message("Video available for file, displaying.")
            self.video = QVideoWidget()
            self.textEdit.player.setVideoOutput(self.video)
            self.videoLayout.addWidget(self.video)
            self.dockAudio.setVisible(True) 
            self.actionShowVideo.setEnabled(True)

    def show_hide_video(self):
        """Hide video if shown.
        """
        if self.video.isVisible():
            log.debug("Hide video.")
            self.video.hide()
        else:
            log.debug("Show video.")
            self.video.show()

    def record_or_pause(self):
        """Toggle between record / pause for the audio recorder."""
        # 1) refuse to record while playback is running
        if self.textEdit.player.playbackState() != QMediaPlayer.StoppedState:
            self.display_message("Playback is in progress. Stop the media first.")
            return

        # ------------------------------------------------------------------
        # RECORD: recorder currently stopped
        # ------------------------------------------------------------------
        if self.textEdit.recorder.recorderState() == QMediaRecorder.StoppedState:
            # show settings dialog
            self.recorder_settings = recorderDialogWindow(self.textEdit.recorder)
            if self.recorder_settings.exec_() != QDialog.Accepted:
                return  # user cancelled

            self.actionStopRecording.setEnabled(True)

            # --- collect the user’s choices --------------------------------
            audio_input   = self.recorder_settings.audio_device.currentText()
            audio_codec   = self.recorder_settings.audio_codec.currentText()
            container     = self.recorder_settings.audio_container.currentText()
            sample_rate   = int(self.recorder_settings.audio_sample_rate.currentText())
            channels      = self.recorder_settings.audio_channels.currentData()
            bitrate       = self.recorder_settings.audio_bitrate.currentData()
            encoding_mode = (
                QMediaRecorder.ConstantQualityEncoding
                if self.recorder_settings.constant_quality.isChecked()
                else QMediaRecorder.ConstantBitRateEncoding
            )
            quality_enum  = QMediaRecorder.Quality(self.recorder_settings.quality_slider.value())

            # --- build QMediaFormat (Qt 6 replacement for *EncoderSettings) -
            fmt = QMediaFormat()

            # codec ----------------------------------------------------------
            # QMediaFormat expects an enum; map the user string if possible,
            # otherwise leave codec unset and rely on container/extension.
            codec_map = {
                "audio/mpeg": QMediaFormat.AudioCodec.MP3,
                "audio/aac":  QMediaFormat.AudioCodec.AAC,
                "audio/flac": QMediaFormat.AudioCodec.FLAC,
                "audio/opus": QMediaFormat.AudioCodec.Opus,
                "audio/wav":  QMediaFormat.AudioCodec.Wave
            }
            if audio_codec in codec_map:
                fmt.setAudioCodec(codec_map[audio_codec])

            # container ------------------------------------------------------
            container_map = {
                "audio/mpeg": QMediaFormat.Mpeg,
                "audio/aac":  QMediaFormat.Mpeg4Audio,
                "audio/flac": QMediaFormat.Flac,
                "audio/ogg":  QMediaFormat.Ogg,
                "audio/wav":  QMediaFormat.Wave
            }
            if container in container_map:
                fmt.setFileFormat(container_map[container])

            # ----------------------------------------------------------------
            self.textEdit.recorder.setAudioInput(audio_input)
            self.textEdit.recorder.setMediaFormat(fmt)
            self.textEdit.recorder.setAudioSampleRate(sample_rate)
            self.textEdit.recorder.setAudioChannelCount(channels)
            self.textEdit.recorder.setAudioBitRate(bitrate)
            self.textEdit.recorder.setEncodingMode(encoding_mode)
            self.textEdit.recorder.setQuality(quality_enum)

            # choose an output file name -------------------------------------
            common_ext = ["aac", "amr", "flac", "gsm", "m4a", "mp3", "ogg",
                          "opus", "raw", "wav"]
            guessed = [ext for ext in common_ext if ext in container]
            out_path = (
                self.textEdit.file_name / "audio" /
                (self.textEdit.file_name.stem + (("." + guessed[0]) if guessed else ""))
            )
            out_path.parent.mkdir(exist_ok=True)
            if out_path.exists():
                if QMessageBox.question(
                        self, "Record",
                        "File exists. Overwrite?",
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.No) != QMessageBox.Yes:
                    self.display_message("Recording cancelled by user.")
                    return

            self.textEdit.recorder.setOutputLocation(QUrl.fromLocalFile(str(out_path)))

            # start recording ------------------------------------------------
            self.textEdit.recorder.record()
            self.textEdit.recorder.durationChanged.connect(self.update_record_time)
            self.display_message("Recording started.")

        # ------------------------------------------------------------------
        # PAUSE
        # ------------------------------------------------------------------
        elif self.textEdit.recorder.recorderState() == QMediaRecorder.RecordingState:
            self.textEdit.recorder.pause()
            self.display_message("Recording paused.")

        # ------------------------------------------------------------------
        # CONTINUE
        # ------------------------------------------------------------------
        else:
            self.textEdit.recorder.record()
            self.display_message("Recording continued.")
            
    def stop_record(self):
        """Stop recording.
        """
        self.textEdit.recorder.stop()
        self.actionStopRecording.setEnabled(False)
        self.display_message("Recording stopped.")
        self.textEdit.recorder.durationChanged.disconnect()

    def setup_caption_window(self, display_font, max_blocks):
        """Set a new window for displaying captions.

        :param display_font: ``QFont``
        :param max_blocks: maximum number of lines to display
        """
        self.caption_window = QMainWindow(self)
        self.caption_window.setMinimumSize(50, 50)
        self.caption_window.setWindowFlags(self.caption_window.windowFlags() | Qt.WindowStaysOnTopHint)
        self.caption_window.setWindowFlags(self.caption_window.windowFlags() | QtCore.Qt.CustomizeWindowHint)
        self.caption_window.setWindowFlags(self.caption_window.windowFlags() & ~QtCore.Qt.WindowCloseButtonHint)
        self.caption_window.setWindowTitle("Plover2CAT Captions")
        self.caption_edit = QPlainTextEdit()
        self.caption_edit.setReadOnly(True)
        self.caption_edit.setCursorWidth(5)
        self.caption_edit.moveCursor(QTextCursor.End)
        self.caption_edit.ensureCursorVisible()
        # use same font for tape and caption
        self.caption_edit.document().setDefaultFont(display_font)
        if max_blocks != 0:
            self.caption_edit.document().setMaximumBlockCount(max_blocks)
        self.caption_window.setCentralWidget(self.caption_edit)
        self.caption_window.show()

    def add_cap(self, cap):
        """Display caption in caption window.
        """
        self.caption_edit.setPlainText(cap)
        self.caption_edit.ensureCursorVisible()

    def setup_captions(self, checked):
        """Set up or clean up caption worker and window.
        """
        if checked:
            res = self.caption_dialog.exec_()
            # need to always keep cursor at end, so can undo from end, but never from middle
            self.actionCursorAtEnd.setChecked(True)
            if res:
                self.setup_caption_window(self.caption_dialog.font, self.caption_dialog.maxDisplayLines.value())
                # if captions are enabled in middle of document, don't start from beginning
                self.caption_cursor_pos = self.textEdit.textCursor().position()
                self.thread = QThread()
                self.cap_worker = captionWorker(max_length = self.caption_dialog.capLength.value(), max_lines = self.caption_dialog.maxDisplayLines.value(),
                                    remote = self.caption_dialog.remoteCapHost.currentText(), endpoint = self.caption_dialog.hostURL.text(), 
                                    port = self.caption_dialog.serverPort.text(), password = self.caption_dialog.serverPassword.text())
                self.cap_worker.moveToThread(self.thread)
                self.thread.started.connect(self.cap_worker.make_caps)
                self.cap_worker.finished.connect(self.thread.quit)
                self.cap_worker.finished.connect(self.cap_worker.deleteLater)
                self.cap_worker.capSend.connect(self.add_cap)
                self.cap_worker.postMessage.connect(self.statusBar.showMessage)
                self.thread.start()
            else:
                self.actionCaptioning.setChecked(False)
        else:
            # do cleanup
            self.cap_worker.clean_and_stop()
            self.caption_edit.clear()
            self.caption_window.hide()

    def display_captions(self):
        """Send captions for display if enabled.
        """
        if not self.actionCaptioning.isChecked():
            return
        current_cursor = self.textEdit.textCursor()
        if self.caption_dialog.enableTimeBuffer.isChecked():
            now_time = datetime.now()
            buffer = timedelta(milliseconds = self.caption_dialog.timeOffset.value())
            # time_limit = now_time - buffer
            current_cursor.setPosition(self.caption_cursor_pos)
            current_block = current_cursor.block()
            stroke_data = current_block.userData()["strokes"]
            track_pos = current_block.position()
            # print(f"initial: {track_pos}")
            while True:
                # this loop can be slow if enormous paragraph
                for el in stroke_data.data:
                    el_time = datetime.strptime(el.time, "%Y-%m-%dT%H:%M:%S.%f")
                    if now_time - el_time > buffer:
                        track_pos += len(el)
                    else:
                        # break on first time encountering element younger
                        break
                current_block = current_block.next()
                if not current_block.isValid():
                    # break if reach end of document in worst case
                    break
                else:
                    if not current_block.userData():
                        break
                    stroke_data = current_block.userData()["strokes"]
                    track_pos = current_block.position()
            current_cursor.setPosition(track_pos, QTextCursor.MoveAnchor)
            # current_cursor.movePosition(QTextCursor.PreviousWord, QTextCursor.MoveAnchor, 2)
        else:
            current_cursor.movePosition(QTextCursor.PreviousWord, QTextCursor.MoveAnchor, self.caption_dialog.charOffset.value())
        new_pos = current_cursor.position()
        if self.caption_cursor_pos >= new_pos:
            return
        current_cursor.setPosition(self.caption_cursor_pos, QTextCursor.KeepAnchor)
        new_text = current_cursor.selectedText()
        self.caption_cursor_pos = new_pos
        self.cap_worker.intake(new_text)

    def flush_caption(self):
        """Send all remaining text up to cursor to caption display.
        """
        old_pos = self.caption_cursor_pos
        current_cursor = self.textEdit.textCursor()
        current_cursor.setPosition(old_pos) 
        current_cursor.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
        new_text = current_cursor.selectedText()
        if new_text == "":
            self.cap_worker.intake("\n" + "\u2029")
        else:
            self.cap_worker.intake(new_text)
        self.caption_cursor_pos = current_cursor.selectionEnd()

    def export_text(self):
        """Export transcript to text file.
        """
        selected_folder = pathlib.Path(self.textEdit.file_name)  / "export"
        selected_file = QFileDialog.getSaveFileName(
            self,
            _("Export Transcript"),
            str(selected_folder.joinpath(self.textEdit.file_name.stem).with_suffix(".txt"))
            , _("Transcript (*.txt)")
        )
        if not selected_file[0]:
            return
        if self.thread and self.thread.isRunning():
            QMessageBox.warning(self, "Plover2CAT", "Another export is in process.")
            return
        contents = self.textEdit.document().toPlainText()
        file_path = pathlib.Path(selected_file[0])
        log.debug("Exporting plain text to %s.", str(file_path))
        with open(file_path, "w") as f:
            f.write(contents)
            self.statusBar.showMessage("Exported in plain text format")

    def export_tape(self):
        """Export tape to text file.
        """
        selected_folder = pathlib.Path(self.textEdit.file_name) / "export"
        selected_file = QFileDialog.getSaveFileName(
            self,
            _("Export Transcript"),
            str(selected_folder.joinpath(self.textEdit.file_name.stem).with_suffix(".tape"))
            , _("Tape (*.tape)")
        )
        if not selected_file[0]:
            return
        tape_contents = self.strokeList.document().toPlainText()
        tape_lines = tape_contents.splitlines()
        doc_lines = []
        for line in tape_lines:
            doc_lines.append(line.split("|")[3])
        with open(selected_file[0], "w", encoding = "utf-8") as f:
            for line in doc_lines:
                f.write(f"{line}\n")

    def export_ascii(self):
        """Export transcript to formatted ASCII file.
        """        
        selected_folder = pathlib.Path(self.textEdit.file_name) / "export"
        selected_file = QFileDialog.getSaveFileName(
            self,
            _("Export Transcript"),
            str(selected_folder.joinpath(self.textEdit.file_name.stem).with_suffix(".txt"))
            , _("Transcript (*.txt)")
        )
        if not selected_file[0]:
            return
        if self.thread and self.thread.isRunning():
            QMessageBox.warning(self, "Plover2CAT", "Another export is in process.")
            return        
        self.save_file()
        log.debug(f"Exporting in ASCII to {selected_file[0]}")
        self.thread = QThread()
        self.progressBar = QProgressBar(self)
        self.progressBar.setMaximum(len(self.textEdit.backup_document))
        self.progressBar.setFormat("Export transcript paragraph %v")
        self.statusBar.addWidget(self.progressBar)
        self.progressBar.show()
        self.worker = documentWorker(deepcopy(self.textEdit.backup_document), selected_file[0], deepcopy(self.textEdit.config), deepcopy(self.textEdit.styles), deepcopy(self.textEdit.user_field_dict), self.textEdit.file_name)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.save_ascii)
        self.worker.progress.connect(self.progressBar.setValue)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.finished.connect(lambda: self.statusBar.showMessage("Exported in ASCII format."))
        self.thread.start() 

    def export_html(self):
        """Export transcript to HTML file.
        """        
        selected_folder = pathlib.Path(self.textEdit.file_name) / "export"
        selected_file = QFileDialog.getSaveFileName(
            self,
            _("Export Transcript"),
            str(selected_folder.joinpath(self.textEdit.file_name.stem).with_suffix(".html"))
            , _("Transcript (*.html)")
        )
        if not selected_file[0]:
            return
        block = self.textEdit.document().begin()
        if self.thread and self.thread.isRunning():
            QMessageBox.warning(self, "Plover2CAT", "Another export is in process.")
            return        
        self.save_file()
        log.debug(f"Exporting in HTML to {selected_file[0]}")
        self.thread = QThread()
        self.progressBar = QProgressBar(self)
        self.progressBar.setMaximum(len(self.textEdit.backup_document))
        self.progressBar.setFormat("Export transcript paragraph %v")
        self.statusBar.addWidget(self.progressBar)
        self.progressBar.show()
        self.worker = documentWorker(deepcopy(self.textEdit.backup_document), selected_file[0], deepcopy(self.textEdit.config), deepcopy(self.textEdit.styles), deepcopy(self.textEdit.user_field_dict), self.textEdit.file_name)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.save_html)
        self.worker.progress.connect(self.progressBar.setValue)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.finished.connect(lambda: self.statusBar.showMessage("Exported in HTML format."))
        self.thread.start()          

    def export_plain_ascii(self):
        """Export transcript to plain ASCII file.
        """
        selected_folder = pathlib.Path(self.textEdit.file_name) / "export"
        selected_file = QFileDialog.getSaveFileName(
            self,
            _("Export Transcript"),
            str(selected_folder.joinpath(self.textEdit.file_name.stem).with_suffix(".txt"))
            , _("Transcript (*.txt)")
        )
        if not selected_file[0]:
            return
        log.debug(f"Exporting in plain ASCII to {selected_file[0]}")
        if self.thread and self.thread.isRunning():
            QMessageBox.warning(self, "Plover2CAT", "Another export is in process.")
            return        
        self.save_file()        
        self.thread = QThread()
        self.progressBar = QProgressBar(self)
        self.progressBar.setMaximum(len(self.textEdit.backup_document))
        self.progressBar.setFormat("Export transcript paragraph %v")
        self.statusBar.addWidget(self.progressBar)
        self.progressBar.show()
        self.worker = documentWorker(deepcopy(self.textEdit.backup_document), selected_file[0], deepcopy(self.textEdit.config), deepcopy(self.textEdit.styles), deepcopy(self.textEdit.user_field_dict), self.textEdit.file_name)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.save_plain_ascii)
        self.worker.progress.connect(self.progressBar.setValue)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.finished.connect(lambda: self.statusBar.showMessage("Exported in plain ASCII format."))
        self.thread.start()        

    def export_srt(self):
        """Export transcript to SRT formatted file.
        """
        selected_folder = pathlib.Path(self.textEdit.file_name) / "export"
        selected_file = QFileDialog.getSaveFileName(
            self,
            _("Export Transcript"),
            str(selected_folder.joinpath(self.textEdit.file_name.stem).with_suffix(".srt"))
            , _("Captions (*.srt)")
        )
        if not selected_file[0]:
            return
        if self.thread and self.thread.isRunning():
            QMessageBox.warning(self, "Plover2CAT", "Another export is in process.")
            return        
        self.save_file()
        self.thread = QThread()
        self.progressBar = QProgressBar(self)
        self.progressBar.setMaximum(len(self.textEdit.backup_document))
        self.progressBar.setFormat("Export transcript paragraph %v")
        self.statusBar.addWidget(self.progressBar)
        self.progressBar.show()
        self.worker = documentWorker(deepcopy(self.textEdit.backup_document), selected_file[0], deepcopy(self.textEdit.config), deepcopy(self.textEdit.styles), deepcopy(self.textEdit.user_field_dict), self.textEdit.file_name)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.save_srt)
        self.worker.progress.connect(self.progressBar.setValue)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.finished.connect(lambda: self.statusBar.showMessage("Exported in srt format."))
        self.thread.start()
        
    def export_odt(self):   
        """Export transcript to ODF TextDocument.
        """
        selected_folder = pathlib.Path(self.textEdit.file_name) / "export"
        selected_file = QFileDialog.getSaveFileName(
            self,
            _("Export Transcript"),
            str(selected_folder.joinpath(self.textEdit.file_name.stem).with_suffix(".odt"))
            , _("OpenDocumentText (*.odt)")
        )
        if not selected_file[0]:
            return
        log.debug(f"Exporting in ODF to {selected_file[0]}")
        # automatically update config and save in case changes were not saved before
        if self.thread and self.thread.isRunning():
            QMessageBox.warning(self, "Plover2CAT", "Another export is in process.")
            return        
        self.save_file()
        self.thread = QThread()
        self.progressBar = QProgressBar(self)
        self.progressBar.setMaximum(len(self.textEdit.backup_document))
        self.progressBar.setFormat("Export transcript paragraph %v")
        self.statusBar.addWidget(self.progressBar)
        self.progressBar.show()
        self.worker = documentWorker(deepcopy(self.textEdit.backup_document), selected_file[0], deepcopy(self.textEdit.config), deepcopy(self.textEdit.styles), deepcopy(self.textEdit.user_field_dict), self.textEdit.file_name)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.save_odf)
        self.worker.progress.connect(self.progressBar.setValue)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.finished.connect(lambda: self.statusBar.showMessage("Exported in Open Document Format."))
        self.thread.start()

    def export_rtf(self):
        """Export transcript to RTF/CRE file.
        """        
        selected_folder = pathlib.Path(self.textEdit.file_name) / "export"
        selected_file = QFileDialog.getSaveFileName(
            self,
            _("Export Transcript"),
            str(selected_folder.joinpath(self.textEdit.file_name.stem).with_suffix(".rtf"))
            , _("RTF/CRE (*.rtf)")
        )
        if not selected_file[0]:
            return
        log.debug(f"Exporting in RTF to {selected_file[0]}")            
        # automatically update config and save in case changes were not saved before
        if self.thread and self.thread.isRunning():
            QMessageBox.warning(self, "Plover2CAT", "Another export is in process.")
            return        
        self.save_file()
        self.thread = QThread()
        self.progressBar = QProgressBar(self)
        self.progressBar.setMaximum(len(self.textEdit.backup_document))
        self.progressBar.setFormat("Export transcript paragraph %v")
        self.statusBar.addWidget(self.progressBar)
        self.progressBar.show()
        self.worker = documentWorker(deepcopy(self.textEdit.backup_document), selected_file[0], deepcopy(self.textEdit.config), deepcopy(self.textEdit.styles), deepcopy(self.textEdit.user_field_dict), self.textEdit.file_name)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.save_rtf)
        self.worker.progress.connect(self.progressBar.setValue)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.finished.connect(lambda: self.statusBar.showMessage("Exported in RTF/CRE format."))
        self.thread.start()
