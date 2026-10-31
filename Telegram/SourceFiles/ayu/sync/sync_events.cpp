// This is the source code of TypendruckerGram, based on AyuGram for Desktop.
#include "ayu/sync/sync_events.h"

#include "ayu/sync/sync_crypto.h"
#include "data/stickers/data_custom_emoji.h"

#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>

#include <algorithm>
#include <set>

namespace AyuSync {
namespace {

constexpr auto kProtocolVersion = 1;

struct EntityName {
	EntityType type;
	const char *name;
};

constexpr EntityName kEntityNames[] = {
	{ EntityType::Bold, "bold" },
	{ EntityType::Italic, "italic" },
	{ EntityType::Underline, "underline" },
	{ EntityType::StrikeOut, "strike" },
	{ EntityType::Spoiler, "spoiler" },
	{ EntityType::Code, "code" },
	{ EntityType::Pre, "pre" },
	{ EntityType::Url, "url" },
	{ EntityType::CustomUrl, "textUrl" },
	{ EntityType::Mention, "mention" },
	{ EntityType::MentionName, "mentionName" },
	{ EntityType::Hashtag, "hashtag" },
	{ EntityType::Cashtag, "cashtag" },
	{ EntityType::BotCommand, "botCommand" },
	{ EntityType::Email, "email" },
	{ EntityType::Phone, "phone" },
	{ EntityType::CustomEmoji, "customEmoji" },
	{ EntityType::Blockquote, "blockquote" },
};

[[nodiscard]] const char *TypeName(EventType type) {
	switch (type) {
	case EventType::Message: return "message";
	case EventType::Edit: return "edit";
	case EventType::Delete: return "delete";
	}
	return "";
}

// Qt 6 keeps JSON integers exact as qint64; never go through double.
[[nodiscard]] int64 Int(const QJsonObject &object, const char *key) {
	return object.value(QLatin1String(key)).toVariant().toLongLong();
}

[[nodiscard]] QJsonObject MediaToJson(const Media &media) {
	return QJsonObject{
		{ "blobId", media.blobId },
		{ "mime", media.mime },
		{ "kind", media.kind },
		{ "name", media.name },
		{ "size", qint64(media.size) },
	};
}

[[nodiscard]] Media MediaFromJson(const QJsonObject &object) {
	return Media{
		.blobId = object.value("blobId").toString(),
		.mime = object.value("mime").toString(),
		.kind = object.value("kind").toString(),
		.name = object.value("name").toString(),
		.size = Int(object, "size"),
	};
}

[[nodiscard]] QJsonObject SnapshotToJson(const Snapshot &snapshot) {
	auto result = QJsonObject{
		{ "dialog", qint64(snapshot.dialog) },
		{ "msgId", snapshot.msgId },
		{ "fromId", qint64(snapshot.fromId) },
		{ "date", snapshot.date },
		{ "editDate", snapshot.editDate },
		{ "topic", qint64(snapshot.topic) },
		{ "text", snapshot.text },
		{ "entities", snapshot.entities },
		{ "replyTo", snapshot.replyTo },
		{ "groupedId", qint64(snapshot.groupedId) },
	};
	if (snapshot.media) {
		result.insert("media", MediaToJson(*snapshot.media));
	}
	return result;
}

[[nodiscard]] std::optional<Snapshot> SnapshotFromJson(const QJsonValue &value) {
	if (!value.isObject()) {
		return std::nullopt;
	}
	const auto object = value.toObject();
	if (!object.contains("dialog") || !object.contains("msgId")) {
		return std::nullopt;
	}
	auto result = Snapshot{
		.dialog = Int(object, "dialog"),
		.msgId = int32(Int(object, "msgId")),
		.fromId = Int(object, "fromId"),
		.date = int32(Int(object, "date")),
		.editDate = int32(Int(object, "editDate")),
		.topic = Int(object, "topic"),
		.text = object.value("text").toString(),
		.entities = object.value("entities").toArray(),
		.replyTo = int32(Int(object, "replyTo")),
		.groupedId = Int(object, "groupedId"),
	};
	if (object.value("media").isObject()) {
		result.media = MediaFromJson(object.value("media").toObject());
	}
	return result;
}

[[nodiscard]] std::string DedupKey(
		int64 account,
		int64 dialog,
		int64 msgId,
		int64 editDate,
		EventType type) {
	return Crypto::Sha256Hex(
		std::to_string(account) + '|'
		+ std::to_string(dialog) + '|'
		+ std::to_string(msgId) + '|'
		+ std::to_string(editDate) + '|'
		+ TypeName(type));
}

} // namespace

QByteArray EncodeEvent(const Event &event) {
	auto object = QJsonObject{
		{ "v", kProtocolVersion },
		{ "type", QString::fromLatin1(TypeName(event.type)) },
		{ "account", qint64(event.account) },
	};
	switch (event.type) {
	case EventType::Message:
		object.insert("snapshot", SnapshotToJson(*event.snapshot));
		break;
	case EventType::Edit:
		object.insert("previous", SnapshotToJson(*event.previous));
		object.insert("newEditDate", event.newEditDate);
		break;
	case EventType::Delete: {
		auto ids = QJsonArray();
		for (const auto id : event.msgIds) {
			ids.push_back(id);
		}
		auto snapshots = QJsonArray();
		for (const auto &snapshot : event.snapshots) {
			snapshots.push_back(SnapshotToJson(snapshot));
		}
		object.insert("dialog", qint64(event.dialog));
		object.insert("msgIds", ids);
		object.insert("snapshots", snapshots);
		object.insert("date", event.date);
	} break;
	}
	return QJsonDocument(object).toJson(QJsonDocument::Compact);
}

std::optional<Event> DecodeEvent(const QByteArray &json) {
	auto error = QJsonParseError();
	const auto document = QJsonDocument::fromJson(json, &error);
	if (error.error != QJsonParseError::NoError || !document.isObject()) {
		return std::nullopt;
	}
	const auto object = document.object();
	const auto type = object.value("type").toString();
	auto result = Event{ .account = Int(object, "account") };
	if (!result.account) {
		return std::nullopt;
	}
	if (type == u"message"_q) {
		result.type = EventType::Message;
		result.snapshot = SnapshotFromJson(object.value("snapshot"));
		if (!result.snapshot) {
			return std::nullopt;
		}
	} else if (type == u"edit"_q) {
		result.type = EventType::Edit;
		result.previous = SnapshotFromJson(object.value("previous"));
		result.newEditDate = int32(Int(object, "newEditDate"));
		if (!result.previous) {
			return std::nullopt;
		}
	} else if (type == u"delete"_q) {
		result.type = EventType::Delete;
		result.dialog = Int(object, "dialog");
		result.date = int32(Int(object, "date"));
		for (const auto &id : object.value("msgIds").toArray()) {
			result.msgIds.push_back(int32(id.toVariant().toLongLong()));
		}
		for (const auto &value : object.value("snapshots").toArray()) {
			if (auto snapshot = SnapshotFromJson(value)) {
				result.snapshots.push_back(std::move(*snapshot));
			}
		}
		if (result.msgIds.empty()) {
			return std::nullopt;
		}
	} else {
		return std::nullopt;
	}
	return result;
}

std::vector<std::string> DedupKeys(const Event &event) {
	switch (event.type) {
	case EventType::Message: {
		const auto &s = *event.snapshot;
		return { DedupKey(event.account, s.dialog, s.msgId, s.editDate, event.type) };
	}
	case EventType::Edit: {
		const auto &p = *event.previous;
		return { DedupKey(event.account, p.dialog, p.msgId, event.newEditDate, event.type) };
	}
	case EventType::Delete: {
		auto result = std::vector<std::string>();
		const auto ids = std::set<int32>(event.msgIds.begin(), event.msgIds.end());
		for (const auto id : ids) {
			result.push_back(DedupKey(event.account, event.dialog, id, 0, event.type));
		}
		return result;
	}
	}
	return {};
}

std::string RelayKey(const Event &event) {
	const auto keys = DedupKeys(event);
	if (keys.size() == 1) {
		return keys.front();
	}
	auto joined = std::string();
	for (const auto &key : keys) {
		if (!joined.empty()) {
			joined += '|';
		}
		joined += key;
	}
	return Crypto::Sha256Hex(joined);
}

QJsonArray EntitiesToJson(const EntitiesInText &entities) {
	auto result = QJsonArray();
	for (const auto &entity : entities) {
		const auto i = std::find_if(
			std::begin(kEntityNames),
			std::end(kEntityNames),
			[&](const EntityName &n) { return n.type == entity.type(); });
		if (i == std::end(kEntityNames)) {
			continue;
		}
		auto item = QJsonObject{
			{ "type", QString::fromLatin1(i->name) },
			{ "offset", entity.offset() },
			{ "length", entity.length() },
		};
		switch (entity.type()) {
		case EntityType::CustomUrl:
			item.insert("url", entity.data());
			break;
		case EntityType::MentionName:
			item.insert(
				"userId",
				qint64(TextUtilities::MentionNameDataToFields(entity.data()).userId));
			break;
		case EntityType::CustomEmoji:
			item.insert(
				"documentId",
				qint64(Data::ParseCustomEmojiData(entity.data())));
			break;
		case EntityType::Pre:
			if (!entity.data().isEmpty()) {
				item.insert("language", entity.data());
			}
			break;
		default:
			break;
		}
		result.push_back(item);
	}
	return result;
}

EntitiesInText EntitiesFromJson(const QJsonArray &entities) {
	auto result = EntitiesInText();
	for (const auto &value : entities) {
		const auto object = value.toObject();
		const auto name = object.value("type").toString();
		const auto i = std::find_if(
			std::begin(kEntityNames),
			std::end(kEntityNames),
			[&](const EntityName &n) { return name == QLatin1String(n.name); });
		if (i == std::end(kEntityNames)) {
			continue;
		}
		auto data = QString();
		switch (i->type) {
		case EntityType::CustomUrl:
			data = object.value("url").toString();
			break;
		case EntityType::MentionName:
			data = TextUtilities::MentionNameDataFromFields({
				.userId = uint64(Int(object, "userId")),
			});
			break;
		case EntityType::CustomEmoji:
			data = Data::SerializeCustomEmojiId(DocumentId(Int(object, "documentId")));
			break;
		case EntityType::Pre:
			data = object.value("language").toString();
			break;
		default:
			break;
		}
		result.push_back(EntityInText(
			i->type,
			int(Int(object, "offset")),
			int(Int(object, "length")),
			data));
	}
	return result;
}

Snapshot SnapshotFromCached(const CachedMessage &message) {
	auto result = Snapshot{
		.dialog = message.dialogId,
		.msgId = message.messageId,
		.fromId = message.fromId,
		.date = message.date,
		.editDate = message.editDate,
		.topic = message.topicId,
		.text = QString::fromStdString(message.text),
		.entities = QJsonDocument::fromJson(
			QByteArray::fromStdString(message.entitiesJson)).array(),
		.replyTo = message.replyMessageId,
		.groupedId = message.groupedId,
	};
	if (!message.mediaKind.empty()) {
		result.media = Media{
			.mime = QString::fromStdString(message.mediaMime),
			.kind = QString::fromStdString(message.mediaKind),
			.name = QString::fromStdString(message.mediaName),
			.size = message.mediaSize,
		};
	}
	return result;
}

CachedMessage CachedFromSnapshot(ID userId, const Snapshot &snapshot, bool channel) {
	auto result = CachedMessage();
	result.userId = userId;
	result.dialogId = snapshot.dialog;
	result.messageId = snapshot.msgId;
	result.channel = channel;
	result.topicId = snapshot.topic;
	result.fromId = snapshot.fromId;
	result.groupedId = snapshot.groupedId;
	result.date = snapshot.date;
	result.editDate = snapshot.editDate;
	result.replyMessageId = snapshot.replyTo;
	result.text = snapshot.text.toStdString();
	result.entitiesJson = QJsonDocument(snapshot.entities)
		.toJson(QJsonDocument::Compact)
		.toStdString();
	if (snapshot.media) {
		result.mediaKind = snapshot.media->kind.toStdString();
		result.mediaMime = snapshot.media->mime.toStdString();
		result.mediaName = snapshot.media->name.toStdString();
		result.mediaSize = snapshot.media->size;
	}
	return result;
}

} // namespace AyuSync
