// This is the source code of TypendruckerGram, based on AyuGram for Desktop.
//
// Plaintext events of TypendruckerGram Sync, see docs/sync/protocol.md.
#pragma once

#include "ayu/data/entities.h"
#include "ui/text/text_entity.h"

#include <QtCore/QJsonArray>

#include <optional>
#include <string>
#include <vector>

namespace AyuSync {

struct Media {
	QString blobId;
	QString mime;
	QString kind;
	QString name;
	int64 size = 0;
};

// One version of one message.
struct Snapshot {
	int64 dialog = 0;
	int32 msgId = 0;
	int64 fromId = 0;
	int32 date = 0;
	int32 editDate = 0;
	int64 topic = 0;
	QString text;
	QJsonArray entities;
	int32 replyTo = 0;
	int64 groupedId = 0;
	std::optional<Media> media;
};

enum class EventType {
	Message,
	Edit,
	Delete,
};

struct Event {
	EventType type = EventType::Message;
	int64 account = 0;
	std::optional<Snapshot> snapshot; // Message
	std::optional<Snapshot> previous; // Edit: the version that was replaced
	int32 newEditDate = 0; // Edit
	int64 dialog = 0; // Delete: channel/supergroup, 0 otherwise
	std::vector<int32> msgIds; // Delete
	std::vector<Snapshot> snapshots; // Delete: known contents
	int32 date = 0; // Delete
};

[[nodiscard]] QByteArray EncodeEvent(const Event &event);
[[nodiscard]] std::optional<Event> DecodeEvent(const QByteArray &json);

// Per-message keys, used to apply events idempotently.
[[nodiscard]] std::vector<std::string> DedupKeys(const Event &event);
// One key per event, used by the relay to drop duplicates.
[[nodiscard]] std::string RelayKey(const Event &event);

[[nodiscard]] QJsonArray EntitiesToJson(const EntitiesInText &entities);
[[nodiscard]] EntitiesInText EntitiesFromJson(const QJsonArray &entities);

[[nodiscard]] Snapshot SnapshotFromCached(const CachedMessage &message);
[[nodiscard]] CachedMessage CachedFromSnapshot(
	ID userId,
	const Snapshot &snapshot,
	bool channel);

} // namespace AyuSync
