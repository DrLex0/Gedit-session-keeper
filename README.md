Gedit Session Keeper
====================

This is a plugin for [Gedit][1], the official text editor of the GNOME desktop
environment. 

It restores the windows that were open the last time Gedit was closed (or forcibly stopped), preserving the open files in those windows and the ordering of their tabs.

Due to limitations of the plugin system, the plugin has to wait 2 seconds before updating changes in session state. This means e.g. if you close a document and then immediately quit Gedit, the document will still be reopened the next time you start Gedit.

This plugin is for Gedit versions 3.28 or above. It might work with lower 3.x versions but this has not been tested. **This plugin is NOT compatible with Gedit 2.x**.


Installation
------------

1. Download the source code from this repository: 

  <https://github.com/Drlex0/gedit-session-keeper>

  You can either use `git clone` or download the code as an archive.

2. Copy these files to your Gedit plugins directory:

    ```
    mkdir -p ~/.local/share/gedit/plugins
    cp -r sessionkeeper.* ~/.local/share/gedit/plugins/
    ```

3. (Re)start Gedit.

4. Activate the plugin: go to `Edit` > `Preferences`, select `Plugins` tab and check `Session Keeper`.

5. Quit and reopen Gedit. This is necessary to initialise and activate the plugin.

[1]: http://www.gedit.org



