// This is the source code of TypendruckerGram, based on AyuGram for Desktop.
//
// TypendruckerGram Sync: shares saved deleted and edited messages with the
// other devices of the owner through the exchange relay, see
// docs/sync/protocol.md.
#pragma once

namespace Main {
class Session;
} // namespace Main

class HistoryItem;

namespace AyuSync {

inline constexpr auto kDefaultRelayUrl = "https://unionofjackbox.tokenized.name/typsync";
inline constexpr auto kDefaultMediaLimit = 20LL * 1024 * 1024;

// Starts the background loop; it idles until sync is set up for an account.
void Start();

// Called right after AyuGram saved the version that an edit replaces.
void OnEdited(not_null<HistoryItem*> item, TimeId newEditDate);
// Called right after AyuGram saved a deleted message.
void OnDeleted(not_null<HistoryItem*> item);

struct Status {
	bool paired = false;
	bool enabled = false;
	QString lastError;
	TimeId lastSyncAt = 0;
	bool trimmed = false;
	int64 mediaLimit = kDefaultMediaLimit;
};

[[nodiscard]] Status GetStatus(not_null<Main::Session*> session);
void SetEnabled(not_null<Main::Session*> session, bool enabled);
void SetMediaLimit(not_null<Main::Session*> session, int64 limit);
void SyncNow();

// Done callbacks get an empty string on success, otherwise an error code.
void CreateOwner(
	not_null<Main::Session*> session,
	const QString &invite,
	Fn<void(QString error)> done);
void Join(
	not_null<Main::Session*> session,
	const QString &code,
	Fn<void(QString error)> done);
void StartPairing(
	not_null<Main::Session*> session,
	Fn<void(QString code, QString error)> done);
void Disconnect(not_null<Main::Session*> session, Fn<void()> done);

} // namespace AyuSync
