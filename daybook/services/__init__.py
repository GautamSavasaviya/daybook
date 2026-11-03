"""Glue between the pure core and the UI: transactions, events, timers and D-Bus.

May use GLib/Gio/GObject; never Gtk, Gdk or Adw (enforced by tests/test_layering.py).
"""
