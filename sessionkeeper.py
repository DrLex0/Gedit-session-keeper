"""
Session Keeper, a session saver for Gedit 3 that simply works.
This is kept as simple as possible because bloat begets bugs.
Currently it only restores files and tab groups per window, no other fancy
things.

This relies on a somewhat ugly approach using timers to discern between user
actions and Gedit closing things while it is quitting. State updates that
aren't certain to be user-initiated, are postponed for a while. If Gedit really
quits, the postponed updates are discarded before they can be executed.
This offers a more consistent result than approaches that attempt to use
GLib.idle_add() or that try to intercept quit events. Those two approaches fail
when there is only one app window and the user closes it. Gedit then quits
without sending any quit actions. Idle calls are also usually still executed in
that case.

Copyright (C) 2019  Alexander Thomas

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
import json
import logging
import os
import threading
import time
import uuid

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gedit', '3.0')

from gi.repository import GObject, GLib, Gio, Gedit


# To facilitate debugging a schema, copy it to /usr/share/glib-2.0/schemas/ and
# run glib-compile-schemas on that dir. Then you can query with gsettings.
SETTINGS_SCHEMA = "be.dr-lex.gedit.plugins.sessionkeeper.gschema"
PLUGIN_PATH = os.path.dirname(os.path.realpath(__file__))
SCHEMAS_PATH = os.path.join(PLUGIN_PATH, 'sessionkeeper.schemas')

SK_LOG = logging.getLogger('SessionKeeper')
SK_LOG.setLevel(logging.INFO)
# For debugging, the following lines are very handy
#SK_LOG.setLevel(logging.DEBUG)
#SK_LOG.addHandler(logging.FileHandler("/tmp/SessionKeeper.log"))


def get_settings():
    try:
        schema_source = Gio.SettingsSchemaSource.new_from_directory(
            SCHEMAS_PATH,
            Gio.SettingsSchemaSource.get_default(),
            False
        )
    except Exception as err:
        SK_LOG.critical("FATAL: could not load schema source from %s: %s", SCHEMAS_PATH, str(err))
        return None

    schema = schema_source.lookup(SETTINGS_SCHEMA, False)
    if not schema:
        SK_LOG.critical("FATAL: could not load settings schema")
        return None
    schema_path = "/" + "/".join(SETTINGS_SCHEMA.split(".")[:-1]) + "/"
    return Gio.Settings.new_full(schema, None, schema_path)


def static_settings(settings):
    if settings:
        return (settings.get_value('exit-timeout').get_double(),
                settings.get_value('launch-timeout').get_double())
    return (1, 1)


class SKeeperAppActivatable(GObject.Object, Gedit.AppActivatable):
    """This class serves no other purpose than to make the code somewhat cleaner
    by having it handle all global logic."""

    __gtype_name__ = 'SKeeperAppActivatable'
    app = GObject.Property(type=Gedit.App)

    settings = get_settings()
    exit_timeout, launch_timeout = static_settings(settings)

    # When activating the plugin for the first time, this will be stuck on True and state
    # will not be saved, hence Gedit must be quitted and reopened to activate the plugin.
    loading = True
    loaded_time = None
    global_timer = None
    global_lock = threading.Lock()
    # One entry per window, key is UUID, value is a list of tab groups in the window
    # that claimed this UUID, each containing a list of file URIs.
    files_per_window = {}
    # Keys are UUIDs, values are dicts with key = timestamp when the state was recorded,
    # value is the same list of tab groups as above (which should always be empty because
    # only pending window delete states should end up in here).
    global_pending = {}

    def __init__(self):
        GObject.Object.__init__(self)
        SK_LOG.info('Created new SessionKeeper AppActivatable, timeouts: %g, %g',
                     self.exit_timeout, self.launch_timeout)

    def do_activate(self):
        SK_LOG.debug('AA received activate event')

    def do_deactivate(self):
        """Another upside of using an AppActivatable is that we have a single
        point to perform remaining global state handling when quitting."""
        SK_LOG.debug('AA received deactivate event')
        SKeeperAppActivatable.process_global_pending()
        SKeeperAppActivatable.save_state()

    @classmethod
    def mark_loaded(cls):
        """Mark loading stage as completed."""
        cls.loading = False
        cls.loaded_time = time.time()

    @classmethod
    def just_loaded(cls):
        """Returns whether mark_loaded was invoked less than launch_timeout ago."""
        if cls.loading:
            return False
        return time.time() - cls.loaded_time < cls.launch_timeout

    @classmethod
    def save_state(cls):
        """Persist the global state of all windows in gsettings."""
        if not cls.settings:
            # Things are badly broken
            return
        with cls.global_lock:
            payload = json.dumps(cls.files_per_window)
            cls.settings.set_value('window-files', GLib.Variant("s", payload))

    @classmethod
    def process_global_pending(cls, now=None):
        """If there are pending global states, apply the youngest ones whose
        timestamp is at least exit_timeout ago."""
        with cls.global_lock:
            if cls.global_timer:
                cls.global_timer.cancel()
                cls.global_timer = None

        changed = False
        with cls.global_lock:
            if not now:
                now = time.time()
            for win_id in cls.global_pending:
                pending_states = cls.global_pending[win_id]
                new_pending_states = {}
                for stamp in sorted(pending_states, reverse=True):
                    if now - stamp < cls.exit_timeout:
                        new_pending_states[stamp] = pending_states[stamp]
                        continue
                    if pending_states[stamp]:
                        cls.files_per_window[win_id] = pending_states[stamp]
                        SK_LOG.debug('  applied new global state for %s', win_id)
                    else:
                        if win_id in cls.files_per_window:
                            del cls.files_per_window[win_id]
                        SK_LOG.debug('  cleared global state for %s', win_id)
                    changed = True
                    break

                cls.global_pending[win_id] = new_pending_states

            cls.global_pending = {win_id: value
                                  for (win_id, value) in cls.global_pending.items() if value}
        if changed:
            cls.save_state()

    @classmethod
    def schedule_global_pending(cls):
        """Schedule a process_global_pending call slightly beyond exit_timeout.
        No global timer must be pending when calling this."""
        SK_LOG.debug("Scheduling global_pending...")
        with cls.global_lock:
            cls.global_timer = threading.Timer(0.1 + cls.exit_timeout, cls.process_global_pending)
            cls.global_timer.start()

    @classmethod
    def dump_final_states(cls, win_id, pending_states):
        """Schedule an update for the final state of window with the given win_id."""
        cls.process_global_pending()
        with cls.global_lock:
            cls.global_pending[win_id] = pending_states
        cls.schedule_global_pending()


class SKeeperWindowActivatable(GObject.Object, Gedit.WindowActivatable):
    """One of these will be created for every Gedit window."""

    __gtype_name__ = "SKeeperWindowActivatable"
    window = GObject.property(type=Gedit.Window)

    def __init__(self):
        GObject.Object.__init__(self)
        self._handlers = []
        self._launch_handler = None
        # Hash, key is timestamp, value is list of files
        self.pending_states = {}
        self.uuid = str(uuid.uuid1())
        SK_LOG.debug('Created new WA instance %s', self.uuid)
        self.pending_timer = None
        self.timer_lock = threading.Lock()

    def process_pending(self, now=None):
        """If there are pending states, apply the youngest one whose timestamp is
        at least exit_timeout ago."""
        # Only one timer must be active at any time. By cancelling a pending timer,
        # a state update may in the worst case be further delayed by exit_timeout.
        with self.timer_lock:
            # Avoid race condition between checking for the timer and cancelling/wiping it.
            # Note that I'm a n00b when it comes to threads in Python and I have no idea
            # whether this is necessary or the best way to do it.
            if self.pending_timer:
                self.pending_timer.cancel()
                self.pending_timer = None

        if not now:
            now = time.time()

        SK_LOG.debug("process_pending in %s", self.uuid)
        changed = False
        new_pending_states = {}
        for stamp in sorted(self.pending_states, reverse=True):
            if now - stamp < SKeeperAppActivatable.exit_timeout:
                new_pending_states[stamp] = self.pending_states[stamp]
                continue
            SKeeperAppActivatable.files_per_window[self.uuid] = self.pending_states[stamp]
            changed = True
            SK_LOG.debug('  applied new state for %s', self.uuid)
            break

        self.pending_states = new_pending_states
        SK_LOG.debug('  pending states left: %d', len(self.pending_states))
        if changed:
            SKeeperAppActivatable.save_state()

    def schedule_pending(self):
        """Schedule a process_pending call slightly beyond exit_timeout.
        No timer must be pending when calling this."""
        SK_LOG.debug("Scheduling process_pending...")
        with self.timer_lock:
            self.pending_timer = threading.Timer(0.1 + SKeeperAppActivatable.exit_timeout,
                                                 self.process_pending)
            self.pending_timer.start()

    def cancel_pending(self):
        """Drop any pending states and timers, in case we're certain about our current state."""
        with self.timer_lock:
            if self.pending_timer:
                self.pending_timer.cancel()
                self.pending_timer = None
        self.pending_states = {}

    def get_state(self):
        """Obtain the list of files for this instance's window.'"""
        state = []
        current_group = None
        tab_group_index = -1

        for document in self.window.get_documents():
            tab_group = Gedit.Tab.get_from_document(document).get_parent()
            if tab_group != current_group:
                tab_group_index += 1
                current_group = tab_group
                state.append([])
            gfile = document.get_location()
            if gfile:
                uri = gfile.get_uri()
                if uri:
                    state[tab_group_index].append(gfile.get_uri())
        return state

    def _register_handler(self, handler, call):
        """Shortcut"""
        self._handlers.append(self.window.connect(handler, call))

    def do_activate(self):
        """Connect signal handlers."""
        SK_LOG.debug('do_activate %s', self.uuid)

        self._register_handler("delete-event", self.on_window_delete_event)
        self._register_handler("tab-added", self.on_tab_add_event)
        self._register_handler("tab-removed", self.on_tab_change_event)
        self._register_handler("tabs-reordered", self.on_tab_change_event)
        self._register_handler("active-tab-state-changed", self.on_tab_change_event)

        # Temporary handler for window initialisation
        self._launch_handler = self.window.connect("show", self.on_window_show)

    def do_deactivate(self):
        """Invoked when this window closes, somewhat like a destructor."""
        SK_LOG.debug('do_deactivate %s', self.uuid)
        for handler_id in self._handlers:
            self.window.disconnect(handler_id)
        self.process_pending()
        if self.pending_states:
            # Migrate any remaining pending states (should be []) to the global state.
            SKeeperAppActivatable.dump_final_states(self.uuid, self.pending_states)

    def on_window_delete_event(self, _window, _event, _data=None):
        """As far as I can see from debugging, this is only invoked when the user
        closes a window, not when quitting the entire app."""
        timestamp = time.time()
        SK_LOG.warning('window_delete_event in %s', self.uuid)
        self.process_pending(timestamp)
        self.pending_states[timestamp] = []
        # Do not schedule anything, this will happen in dump_final_states
        return False

    def close_empty_tab(self):
        """If the active tab is not a loaded file, close it."""
        tab = self.window.get_active_tab()
        if not tab.get_document().get_location() and tab.get_state() == 0:
            SK_LOG.debug('Closing empty tab')
            self.window.close_tab(tab)

    def on_tab_add_event(self, _window, tab=None):
        """When adding a tab, immediately apply the new state."""
        if SKeeperAppActivatable.loading:
            return False
        SK_LOG.debug('on_tab_add_event in %s', self.uuid)
        if SKeeperAppActivatable.just_loaded():
            # For consistency, bring this window to front, especially desirable if Gedit
            # was launched by opening a file
            GLib.idle_add(self.window.present)

            if not tab.get_document().get_location():
                # Yet another timer heuristic to detect the default empty tab being
                # opened right after the plugin has loaded everything.
                if SKeeperAppActivatable.files_per_window:
                    SK_LOG.debug('  scheduling removal of default empty tab')
                    # We can't just call close_tab(tab) here: causes a core dump, maybe because
                    # Gedit still tries to do something with the tab after sending this event
                    GLib.idle_add(self.close_empty_tab)
                # Even if we keep the tab, we're not interested in it
                return False

        self.cancel_pending()
        SKeeperAppActivatable.files_per_window[self.uuid] = self.get_state()
        SKeeperAppActivatable.save_state()
        return False

    def on_tab_change_event(self, _window, _tab=None):
        """Changed tab state, could be a remove.
        Create a pending state because we can't be sure whether this state needs
        to be persisted until exit_timeout has expired."""
        if SKeeperAppActivatable.loading:
            return False
        timestamp = time.time()
        SK_LOG.debug('on_tab_change_event in %s', self.uuid)
        self.process_pending(timestamp)
        self.pending_states[timestamp] = self.get_state()
        self.schedule_pending()
        return False

    def on_window_show(self, _window, _data=None):
        """A newly spawned instance looks for any unclaimed IDs in the
        global_config. If there are any, then grab one and load its files,
        otherwise mark the loading phase as finished."""
        SK_LOG.debug('on_window_show')
        if not SKeeperAppActivatable.loading:
            SK_LOG.debug('  ... but no longer loading')
            return

        if not SKeeperAppActivatable.settings:
            return
        payload = SKeeperAppActivatable.settings.get_value('window-files').get_string()
        SK_LOG.debug('  JSON dump read: %s', payload)

        saved_state = {}
        if payload:
            try:
                saved_state = json.loads(payload)
                if not isinstance(saved_state, dict):
                    raise TypeError("Value is not a dict")
            # If anyone feels like filtering out only the expected exceptions, feel free.
            # At this time I'm just lazy.
            #except (UnicodeDecodeError, json.JSONDecodeError) as err:
            except Exception as err:
                SK_LOG.error("Discarding previously saved state because it fails to decode: %s",
                             str(err))
        else:
            SK_LOG.info('  No previous state, starting fresh')

        SK_LOG.debug("  Number of windows in config: %d", len(saved_state))
        uuids_left = [key for key in saved_state.keys()]
        for win_id in saved_state:
            uuids_left.remove(win_id)
            if win_id in SKeeperAppActivatable.files_per_window:
                continue
            groups = saved_state[win_id]
            if not groups:
                continue

            SK_LOG.debug("  Claiming %s", win_id)
            self.uuid = win_id
            SK_LOG.debug("    Groups in %s: %d", win_id, len(groups))
            group_number = 0
            for files in groups:
                group_number += 1
                SK_LOG.debug("      Files in group %d: %d", group_number, len(files))
                tab_to_be_closed = None
                if group_number > 1:
                    self.window.activate_action('new-tab-group')
                    # The tab group will be created with an empty document
                    tab_to_be_closed = self.window.get_active_tab()

                for document_uri in files:
                    location = Gio.file_new_for_uri(document_uri)
                    self.window.create_tab_from_location(location, None, 0,
                                                         0, False, True)
                    if tab_to_be_closed:
                        self.window.close_tab(tab_to_be_closed)
                        tab_to_be_closed = None

            SKeeperAppActivatable.files_per_window[win_id] = self.get_state()
            break

        if uuids_left:
            # Spawn a new window to claim the next id
            app = Gedit.App.get_default()
            next_window = app.create_window(None)
            next_window.show()
            next_window.activate()
        else:
            SK_LOG.debug("Finished loading")
            SKeeperAppActivatable.mark_loaded()

        self.window.disconnect(self._launch_handler)
