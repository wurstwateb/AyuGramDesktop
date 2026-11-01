// This is the source code of TypendruckerGram, based on AyuGram for Desktop.
#pragma once

namespace Ui {
class GenericBox;
} // namespace Ui

namespace Window {
class SessionController;
} // namespace Window

// Settings of TypendruckerGram Sync: pairing, on/off, media limit, status.
void SyncBox(
	not_null<Ui::GenericBox*> box,
	not_null<Window::SessionController*> controller);
