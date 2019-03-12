"""
Session Keeper, a session saver for Gedit 3 that actually works.
This is kept as simple as possible because bloat begets bugs.
Currently it only restores files per window, no other fancy things.

The tricky part is that there is _no_ simple way to discern between the user closing tabs or
windows, or Gedit closing things because it is quitting. Therefore this plugin relies on timing
and postpones state updates of tab and window close events. If Gedit really quits, the postponed
update is killed before it can be executed. It's slightly ugly but it generally works. Only on
very slow systems this may produce unexpected results.
"""
import json
import logging
import os
import threading
import time
import uuid
from gi.repository import GObject, GLib, Gio, Gedit


# The fuzz factor timeout to discern between user action and gedit quitting
EXIT_TIMEOUT = 2
# Timeout within which we expect gedit to create the default empty document
# after the plugin has finished loading
OPEN_TIMEOUT = 2

# To facilitate debugging a schema, copy it to /usr/share/glib-2.0/schemas/ and
# run glib-compile-schemas on that dir. Then you can query with gsettings.
SETTINGS_SCHEMA = "be.dr-lex.gedit.plugins.sessionkeeper.gschema"
PLUGIN_PATH = os.path.dirname(os.path.realpath(__file__))
SCHEMAS_PATH = os.path.join(PLUGIN_PATH, 'sessionkeeper.schemas')

SK_LOG = logging.getLogger('SessionKeeper')
# For debugging, the following lines are very handy
SK_LOG.setLevel(logging.DEBUG)
SK_LOG.addHandler(logging.FileHandler("/tmp/SessionKeeper.log"))


def get_settings():
    try:
        schema_source = Gio.SettingsSchemaSource.new_from_directory(
            SCHEMAS_PATH,
            Gio.SettingsSchemaSource.get_default(),
            False
        )
    except Exception as err:
        SK_LOG.error("FATAL: could not load schema source from %s: %s", SCHEMAS_PATH, str(err))
        return None

    schema = schema_source.lookup(SETTINGS_SCHEMA, False)
    if not schema:
        SK_LOG.error("FATAL: could not load settings schema")
        return None
    schema_path = "/" + "/".join(SETTINGS_SCHEMA.split(".")[:-1]) + "/"
    return Gio.Settings.new_full(schema, None, schema_path)


class SKeeperAppActivatable(GObject.Object, Gedit.AppActivatable):
    """This class serves no other purpose than to make the code somewhat cleaner
    by having it handle all global logic.
    I had hoped there would be an event signalling that GEdit is about to quit,
    but there is none. This means we still need to rely on timers to discern
    between user actions and the app quitting. Ugh."""

    __gtype_name__ = 'SKeeperAppActivatable'
    app = GObject.Property(type=Gedit.App)

    settings = get_settings()

    # When activating the plugin for the first time, this will be stuck on True and state
    # will not be saved, hence Gedit must be quitted and reopened to activate the plugin.
    loading = True
    loaded_time = None
    global_timer = None
    global_lock = threading.Lock()
    # One entry per window, key is UUID, value is a list of file URIs in the window
    # that claimed this UUID.
    global_state = {}
    # Keys are UUIDs, values are the same kind of dicts as pending_states.
    # This will only contain pending window delete states.
    global_pending = {}

    def __init__(self):
        GObject.Object.__init__(self)
        SK_LOG.debug('Created new AA instance')

    def do_activate(self):
        SK_LOG.debug('AA received activate event')

    def do_deactivate(self):
        """Another upside of using an AppActivatable is that we have a single
        point to perform remaining global state handling when quitting."""
        SK_LOG.debug('AA received deactivate event')
        SKeeperAppActivatable.process_global_pending()
        SKeeperAppActivatable.save_state()

    @classmethod
    def save_state(cls):
        """Persist the global state of all windows in gsettings."""
        with cls.global_lock:
            payload = json.dumps(cls.global_state)
            cls.settings.set_value('state', GLib.Variant("s", payload))

    @classmethod
    def process_global_pending(cls, now=None):
        """If there are pending global states, apply the youngest ones whose
        timestamp is at least EXIT_TIMEOUT ago."""
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
                    if now - stamp < EXIT_TIMEOUT:
                        new_pending_states[stamp] = pending_states[stamp]
                        continue
                    if pending_states[stamp]:
                        cls.global_state[win_id] = pending_states[stamp]
                        SK_LOG.debug('  applied new global state for %s', win_id)
                    else:
                        if win_id in cls.global_state:
                            del cls.global_state[win_id]
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
        """Schedule a process_global_pending call after slightly longer than EXIT_TIMEOUT.
        No global timer must be pending when calling this."""
        SK_LOG.debug("Scheduling global_pending...")
        with cls.global_lock:
            cls.global_timer = threading.Timer(0.1 + EXIT_TIMEOUT, cls.process_global_pending)
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
        at least EXIT_TIMEOUT ago."""
        # Only one timer must be active at any time. By cancelling a pending timer,
        # a state update may in the worst case be further delayed by EXIT_TIMEOUT.
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
            if now - stamp < EXIT_TIMEOUT:
                new_pending_states[stamp] = self.pending_states[stamp]
                continue
            SKeeperAppActivatable.global_state[self.uuid] = self.pending_states[stamp]
            changed = True
            SK_LOG.debug('  applied new state for %s', self.uuid)
            break

        self.pending_states = new_pending_states
        SK_LOG.debug('  pending states left: %d', len(self.pending_states))
        if changed:
            SKeeperAppActivatable.save_state()

    def schedule_pending(self):
        """Schedule a process_pending call after slightly longer than EXIT_TIMEOUT.
        No timer must be pending when calling this."""
        SK_LOG.debug("Scheduling process_pending...")
        with self.timer_lock:
            self.pending_timer = threading.Timer(0.1 + EXIT_TIMEOUT, self.process_pending)
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
        window_uris = []
        for document in self.window.get_documents():
            gfile = document.get_location()
            if gfile:
                uri = gfile.get_uri()
                if uri:
                    window_uris.append(gfile.get_uri())
        return window_uris

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

    def on_tab_add_event(self, window, tab=None):
        """When adding a tab, immediately apply the new state."""
        if SKeeperAppActivatable.loading:
            return False
        SK_LOG.debug('on_tab_add_event in %s', self.uuid)
        if not tab.get_document().get_location():
            # Yet another timer heuristic to detect the default empty tab being
            # opened right after the plugin has loaded everything.
            if (SKeeperAppActivatable.global_state and
                    time.time() - SKeeperAppActivatable.loaded_time < OPEN_TIMEOUT):
                SK_LOG.debug('  killing default empty tab!')
                #window.close_tab(tab)  # FAIL. Causes core dump!?
                return True # whatever!!!
            # Even if we keep the tab, we're not interested in it
            return False

        self.cancel_pending()
        SKeeperAppActivatable.global_state[self.uuid] = self.get_state()
        SKeeperAppActivatable.save_state()
        return False

    def on_tab_change_event(self, _window, _tab=None):
        """Changed tab state, could be a remove.
        Create a pending state because we can't be sure whether this state needs
        to be persisted until EXIT_TIMEOUT has expired."""
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
        payload = SKeeperAppActivatable.settings.get_value('state').get_string()
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
            if win_id in SKeeperAppActivatable.global_state:
                continue
            files = saved_state[win_id]
            if not files:
                continue

            SK_LOG.debug("  Claiming %s", win_id)
            self.uuid = win_id
            SK_LOG.debug("    Files in %s: %d", win_id, len(files))
            for document_uri in files:
                location = Gio.file_new_for_uri(document_uri)
                tab = self.window.get_tab_from_location(location)
                if not tab:
                    self.window.create_tab_from_location(location, None, 0,
                                                         0, False, True)
            SKeeperAppActivatable.global_state[win_id] = self.get_state()
            break

        if uuids_left:
            # Spawn a new window to claim the next id
            app = Gedit.App.get_default()
            next_window = app.create_window(None)
            next_window.show()
            next_window.activate()
        else:
            SK_LOG.debug("Finished loading")
            SKeeperAppActivatable.loading = False
            SKeeperAppActivatable.loaded_time = time.time()

        self.window.disconnect(self._launch_handler)
