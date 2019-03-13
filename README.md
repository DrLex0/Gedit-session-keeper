# Gedit Session Keeper

This is a plugin for [Gedit][1], the official text editor of the GNOME desktop
environment. 

It restores the windows that were open the last time Gedit was closed (or
forcibly stopped), preserving the open files in those windows and the ordering
of their tabs. Tab groups are also restored.

This is intended to be a basic session saver without all possible bells and
whistles. This should reduce the risk that the plugin breaks each time one of
those bells or whistles is changed in a Gedit update. I'm sure there are
fancier session savers, but this one does all I expect from it.

The plugin relies on timings to decide when to update changes in session state.
This means e.g. if you close a document and then immediately quit Gedit, the
document will still be reopened the next time you start Gedit.

This plugin is for Gedit versions 3.28 or above. It might work with lower 3.x
versions but this has not been tested. **This plugin is NOT compatible with
Gedit 2.x.**


## Installation

1. Download the source code from this repository: 

   <https://github.com/Drlex0/gedit-session-keeper>

   You can either use `git clone`, or download the code as an archive.

2. Copy these files to your Gedit plugins directory:

   ```
   mkdir -p ~/.local/share/gedit/plugins
   cp -r sessionkeeper.* ~/.local/share/gedit/plugins/
   ```

3. (Re)start Gedit.

4. Activate the plugin: go to `Preferences`, select `Plugins` tab and check
   `Session Keeper`.

5. Quit and reopen Gedit. This is necessary to initialise and activate the plugin.


## Credits

Inspired by:

* [Restore Tabs][https://github.com/Quixotix/gedit-restore-tabs] by Quixotix
* [Ex-Mortis][https://github.com/jefferyto/gedit-ex-mortis] by Jeffery To


## License

Copyright &copy; 2019 Alexander Thomas (doctor.lex at gmail.com)

Released under GNU General Public License version 3


[1]: <http://www.gedit.org>



