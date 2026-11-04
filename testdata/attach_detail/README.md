Recorded attach sessions of an item-page task (fixtures for test_attach_detail.py).

Four sanitised session files in the recorder's own format (`attach.Recorder.to_session`):
the agent opened the package list, then one package page. The input is the package
name, which is the item URL's last path segment (`/package/<name>`). The network log
keeps each document's response body, which the tests serve as the site's fresh answer.
Synthetic host `pkgs.example.test`; nothing here was fetched from a real site.
