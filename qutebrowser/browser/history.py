# vim: ft=python fileencoding=utf-8 sts=4 sw=4 et:

# Copyright 2015-2017 Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# This file is part of qutebrowser.
#
# qutebrowser is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# qutebrowser is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with qutebrowser.  If not, see <http://www.gnu.org/licenses/>.

"""Simple history which gets written to disk."""

import time
import collections

from PyQt5.QtCore import pyqtSignal, pyqtSlot, QUrl, QObject

from qutebrowser.commands import cmdutils
from qutebrowser.utils import (utils, objreg, standarddir, log, qtutils,
                               usertypes, message)
from qutebrowser.misc import lineparser, objects, sql


class Entry:

    """A single entry in the web history.

    Attributes:
        atime: The time the page was accessed.
        url: The URL which was accessed as QUrl.
        redirect: If True, don't save this entry to disk
    """

    def __init__(self, atime, url, title, redirect=False):
        self.atime = float(atime)
        self.url = url
        self.title = title
        self.redirect = redirect
        qtutils.ensure_valid(url)

    def __repr__(self):
        return utils.get_repr(self, constructor=True, atime=self.atime,
                              url=self.url_str(), title=self.title,
                              redirect=self.redirect)

    def __str__(self):
        atime = str(int(self.atime))
        if self.redirect:
            atime += '-r'  # redirect flag
        elems = [atime, self.url_str()]
        if self.title:
            elems.append(self.title)
        return ' '.join(elems)

    def __eq__(self, other):
        return (self.atime == other.atime and
                self.title == other.title and
                self.url == other.url and
                self.redirect == other.redirect)

    def url_str(self):
        """Get the URL as a lossless string."""
        return self.url.toString(QUrl.FullyEncoded | QUrl.RemovePassword)

    @classmethod
    def from_str(cls, line):
        """Parse a history line like '12345 http://example.com title'."""
        data = line.split(maxsplit=2)
        if len(data) == 2:
            atime, url = data
            title = ""
        elif len(data) == 3:
            atime, url, title = data
        else:
            raise ValueError("2 or 3 fields expected")

        url = QUrl(url)
        if not url.isValid():
            raise ValueError("Invalid URL: {}".format(url.errorString()))

        # https://github.com/qutebrowser/qutebrowser/issues/670
        atime = atime.lstrip('\0')

        if '-' in atime:
            atime, flags = atime.split('-')
        else:
            flags = ''

        if not set(flags).issubset('r'):
            raise ValueError("Invalid flags {!r}".format(flags))

        redirect = 'r' in flags

        return cls(atime, url, title, redirect=redirect)


class WebHistory(sql.SqlTable):

    """The global history of visited pages.

    This is a little more complex as you'd expect so the history can be read
    from disk async while new history is already arriving.

    While reading from disk is still ongoing, the history is saved in
    self._temp_history instead, and then inserted into the sql table once
    the async read completes.

    All history which is new in this session (rather than read from disk from a
    previous browsing session) is also stored in self._new_history.
    self._saved_count tracks how many of those entries were already written to
    disk, so we can always append to the existing data.

    Attributes:
        _lineparser: The AppendLineParser used to save the history.
        _new_history: A list of Entry items of the current session.
        _saved_count: How many HistoryEntries have been written to disk.
        _initial_read_started: Whether async_read was called.
        _initial_read_done: Whether async_read has completed.
        _temp_history: List of history entries from before async_read finished.

    Signals:
        cleared: Emitted after the history is cleared.
    """

    cleared = pyqtSignal()
    async_read_done = pyqtSignal()

    def __init__(self, hist_dir, hist_name, parent=None):
        super().__init__("History", ['url', 'title', 'atime', 'redirect'],
                         primary_key='url', parent=parent)
        self._initial_read_started = False
        self._initial_read_done = False
        self._lineparser = lineparser.AppendLineParser(hist_dir, hist_name,
                                                       parent=self)
        self._temp_history = []
        self._new_history = []
        self._saved_count = 0

    def __repr__(self):
        return utils.get_repr(self, length=len(self))

    def async_read(self):
        """Read the initial history."""
        if self._initial_read_started:
            log.init.debug("Ignoring async_read() because reading is started.")
            return
        self._initial_read_started = True

        with self._lineparser.open():
            for line in self._lineparser:
                yield

                line = line.rstrip()
                if not line:
                    continue

                try:
                    entry = Entry.from_str(line)
                except ValueError as e:
                    log.init.warning("Invalid history entry {!r}: {}!".format(
                        line, e))
                    continue

                # This de-duplicates history entries; only the latest
                # entry for each URL is kept. If you want to keep
                # information about previous hits change the items in
                # old_urls to be lists or change Entry to have a
                # list of atimes.
                self._add_entry(entry)

        self._initial_read_done = True
        self.async_read_done.emit()
        objreg.get('save-manager').add_saveable(
            'history', self.save, self.changed)

        for entry in self._temp_history:
            self._add_entry(entry)
            self._new_history.append(entry)
        self._temp_history.clear()

    def _add_entry(self, entry):
        """Add an entry to the in-memory database."""
        self.insert(entry.url_str(), entry.title, entry.atime, entry.redirect,
                    replace=True)

    def get_recent(self):
        """Get the most recent history entries."""
        old = self._lineparser.get_recent()
        return old + [str(e) for e in self._new_history]

    def save(self):
        """Save the history to disk."""
        new = (str(e) for e in self._new_history[self._saved_count:])
        self._lineparser.new_data = new
        self._lineparser.save()
        self._saved_count = len(self._new_history)

    @cmdutils.register(name='history-clear', instance='web-history')
    def clear(self, force=False):
        """Clear all browsing history.

        Note this only clears the global history
        (e.g. `~/.local/share/qutebrowser/history` on Linux) but not cookies,
        the back/forward history of a tab, cache or other persistent data.

        Args:
            force: Don't ask for confirmation.
        """
        if force:
            self._do_clear()
        else:
            message.confirm_async(self._do_clear, title="Clear all browsing "
                                "history?")

    def _do_clear(self):
        self._lineparser.clear()
        self.delete_all()
        self._temp_history.clear()
        self._new_history.clear()
        self._saved_count = 0
        self.cleared.emit()

    @pyqtSlot(QUrl, QUrl, str)
    def add_from_tab(self, url, requested_url, title):
        """Add a new history entry as slot, called from a BrowserTab."""
        if url.scheme() == 'data' or requested_url.scheme() == 'data':
            return
        if url.isEmpty():
            # things set via setHtml
            return

        no_formatting = QUrl.UrlFormattingOption(0)
        if (requested_url.isValid() and
                not requested_url.matches(url, no_formatting)):
            # If the url of the page is different than the url of the link
            # originally clicked, save them both.
            self.add_url(requested_url, title, redirect=True)
        self.add_url(url, title)

    def add_url(self, url, title="", *, redirect=False, atime=None):
        """Called via add_from_tab when a URL should be added to the history.

        Args:
            url: A url (as QUrl) to add to the history.
            redirect: Whether the entry was redirected to another URL
                      (hidden in completion)
            atime: Override the atime used to add the entry
        """
        if not url.isValid():  # pragma: no cover
            # the no cover pragma is a WORKAROUND for this not being covered in
            # old Qt versions.
            log.misc.warning("Ignoring invalid URL being added to history")
            return

        if atime is None:
            atime = time.time()
        entry = Entry(atime, url, title, redirect=redirect)
        if self._initial_read_done:
            self._add_entry(entry)
            self._new_history.append(entry)
        else:
            self._temp_history.append(entry)


def init(parent=None):
    """Initialize the web history.

    Args:
        parent: The parent to use for WebHistory.
    """
    history = WebHistory(hist_dir=standarddir.data(), hist_name='history',
                         parent=parent)
    objreg.register('web-history', history)

    if objects.backend == usertypes.Backend.QtWebKit:
        from qutebrowser.browser.webkit import webkithistory
        webkithistory.init(history)
