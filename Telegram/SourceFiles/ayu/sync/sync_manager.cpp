// This is the source code of TypendruckerGram, based on AyuGram for Desktop.
#include "ayu/sync/sync_manager.h"

#include "api/api_text_entities.h"
#include "ayu/data/ayu_database.h"
#include "ayu/data/messages_storage.h"
#include "ayu/sync/sync_crypto.h"
#include "ayu/sync/sync_events.h"
#include "ayu/utils/telegram_helpers.h"
#include "base/call_delayed.h"
#include "base/timer.h"
#include "base/unixtime.h"
#include "core/application.h"
#include "core/core_settings.h"
#include "data/data_channel.h"
#include "data/data_chat.h"
#include "data/data_document.h"
#include "data/data_media_types.h"
#include "data/data_peer.h"
#include "data/data_session.h"
#include "history/history.h"
#include "history/history_item.h"
#include "history/history_item_components.h"
#include "main/main_account.h"
#include "main/main_domain.h"
#include "main/main_session.h"
#include "mtproto/mtproto_proxy_data.h"

#include <QtCore/QJsonArray>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QSysInfo>
#include <QtNetwork/QNetworkAccessManager>
#include <QtNetwork/QNetworkProxy>
#include <QtNetwork/QNetworkReply>
#include <QtNetwork/QNetworkRequest>

#include <map>
#include <set>

namespace AyuSync {
namespace {

using Crypto::Bytes;

constexpr auto kTickMs = crl::time(5000);
constexpr auto kPullEveryMs = crl::time(15000);
constexpr auto kHeartbeatEveryMs = crl::time(60000);
constexpr auto kMaxBackoffMs = crl::time(5 * 60 * 1000);
constexpr auto kDeleteBatchMs = crl::time(200);
constexpr auto kOutboxBatch = 100;
constexpr auto kPageSize = 200;

struct UserLoop {
	bool busy = false;
	crl::time lastPull = 0;
	crl::time lastHeartbeat = 0;
	crl::time nextAttempt = 0;
	crl::time backoff = 0;
	QString lastError;
};

struct PendingDelete {
	ID dialog = 0;
	std::vector<int32> msgIds;
	std::vector<Snapshot> snapshots;
};

std::map<ID, UserLoop> Loops;
std::map<std::pair<ID, ID>, PendingDelete> PendingDeletes;

using Done = Fn<void(int status, QByteArray body)>;

// ---- helpers ----

[[nodiscard]] ID UserIdOf(not_null<Main::Session*> session) {
	return ID(session->userId().bare & PeerId::kChatTypeMask);
}

[[nodiscard]] Main::Session *FindSession(ID userId) {
	if (!Core::IsAppLaunched() || !Core::App().domain().started()) {
		return nullptr;
	}
	for (const auto &[index, account] : Core::App().domain().accounts()) {
		if (const auto session = account->maybeSession()) {
			if (UserIdOf(session) == userId) {
				return session;
			}
		}
	}
	return nullptr;
}

[[nodiscard]] Bytes ToBytes(const std::vector<char> &data) {
	return Bytes(data.begin(), data.end());
}

[[nodiscard]] Bytes ToBytes(const QByteArray &data) {
	return Bytes(data.begin(), data.end());
}

[[nodiscard]] std::vector<char> ToChars(const Bytes &data) {
	return std::vector<char>(data.begin(), data.end());
}

[[nodiscard]] QByteArray ToQBytes(const Bytes &data) {
	return QByteArray(reinterpret_cast<const char*>(data.data()), int(data.size()));
}

[[nodiscard]] QString B64(const Bytes &data) {
	return QString::fromLatin1(ToQBytes(data).toBase64());
}

[[nodiscard]] Bytes UnB64(const QString &data) {
	return ToBytes(QByteArray::fromBase64(data.toLatin1()));
}

[[nodiscard]] int NowSeconds() {
	return int(base::unixtime::now());
}

[[nodiscard]] QString DeviceName() {
	const auto name = QSysInfo::prettyProductName();
	return u"TypendruckerGram Desktop ("_q
		+ (name.isEmpty() ? QSysInfo::productType() : name)
		+ u")"_q;
}

[[nodiscard]] std::optional<SyncState> ActiveState(ID userId) {
	auto state = AyuDatabase::getSyncState(userId);
	if (!state || state->token.empty() || state->ownerPrivate.empty()) {
		return std::nullopt;
	}
	return state;
}

QNetworkAccessManager &Network() {
	static auto manager = new QNetworkAccessManager();
	return *manager;
}

void ApplyProxy() {
	const auto &proxy = Core::App().settings().proxy();
	if (proxy.isEnabled()) {
		const auto selected = proxy.selected();
		if (selected.type == MTP::ProxyData::Type::Socks5
			|| selected.type == MTP::ProxyData::Type::Http) {
			Network().setProxy(MTP::ToNetworkProxy(MTP::ToDirectIpProxy(selected)));
			return;
		}
	}
	Network().setProxy(QNetworkProxy(QNetworkProxy::DefaultProxy));
}

void Request(
		const QByteArray &method,
		const std::string &baseUrl,
		const QString &path,
		const std::string &token,
		const QByteArray &body,
		const QByteArray &contentType,
		Done done) {
	ApplyProxy();
	auto base = QString::fromStdString(baseUrl);
	while (base.endsWith('/')) {
		base.chop(1);
	}
	auto request = QNetworkRequest(QUrl(base + path));
	if (!token.empty()) {
		request.setRawHeader("Authorization", "Bearer " + QByteArray::fromStdString(token));
	}
	if (!contentType.isEmpty()) {
		request.setHeader(QNetworkRequest::ContentTypeHeader, contentType);
	}
	request.setTransferTimeout(120000);
	const auto reply = Network().sendCustomRequest(request, method, body);
	QObject::connect(reply, &QNetworkReply::finished, reply, [=] {
		const auto status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
		const auto data = reply->readAll();
		reply->deleteLater();
		done(status, data);
	});
}

void JsonRequest(
		const QByteArray &method,
		const std::string &baseUrl,
		const QString &path,
		const std::string &token,
		const QJsonObject &body,
		Fn<void(int status, QJsonObject object)> done) {
	const auto payload = (method == "GET" || method == "DELETE")
		? QByteArray()
		: QJsonDocument(body).toJson(QJsonDocument::Compact);
	Request(method, baseUrl, path, token, payload, "application/json", [=](int status, QByteArray data) {
		const auto document = QJsonDocument::fromJson(data);
		done(status, document.isObject() ? document.object() : QJsonObject());
	});
}

[[nodiscard]] bool Ok(int status) {
	return status >= 200 && status < 300;
}

[[nodiscard]] QString ErrorOf(int status, const QJsonObject &object) {
	const auto code = object.value("error").toString();
	if (!code.isEmpty()) {
		return code;
	}
	return status ? u"http_%1"_q.arg(status) : u"network"_q;
}

// ---- snapshots ----

[[nodiscard]] std::optional<Media> MediaOf(not_null<HistoryItem*> item) {
	const auto media = item->media();
	if (!media) {
		return std::nullopt;
	}
	auto result = Media();
	if (media->photo()) {
		result.kind = u"photo"_q;
		result.mime = u"image/jpeg"_q;
	} else if (const auto document = media->document()) {
		result.mime = document->mimeString();
		result.size = document->size;
		result.name = document->filename();
		result.kind = document->isVoiceMessage() ? u"voice"_q
			: document->isVideoMessage() ? u"round"_q
			: document->sticker() ? u"sticker"_q
			: document->isAnimation() ? u"animation"_q
			: document->isVideoFile() ? u"video"_q
			: document->isAudioFile() ? u"audio"_q
			: u"file"_q;
	} else {
		return std::nullopt;
	}
	return result;
}

[[nodiscard]] Snapshot SnapshotOf(not_null<HistoryItem*> item) {
	auto result = Snapshot();
	result.dialog = getDialogIdFromPeer(item->history()->peer);
	result.msgId = int32(item->id.bare);
	result.fromId = int64(item->from()->id.value & PeerId::kChatTypeMask);
	result.date = int32(item->date());
	if (const auto edited = item->Get<HistoryMessageEdited>()) {
		result.editDate = int32(edited->date);
	}
	result.topic = item->topic() ? int64(item->topicRootId().bare) : 0;
	const auto text = item->originalText();
	result.text = text.text;
	result.entities = EntitiesToJson(text.entities);
	if (const auto reply = item->Get<HistoryMessageReply>()) {
		result.replyTo = int32(reply->messageId().bare);
	}
	result.groupedId = int64(item->groupId().raw());
	result.media = MediaOf(item);
	return result;
}

[[nodiscard]] std::vector<char> SerializeEntities(
		not_null<Main::Session*> session,
		const QJsonArray &entities) {
	auto parsed = EntitiesFromJson(entities);
	if (parsed.empty()) {
		return {};
	}
	// Mentions are only kept by EntitiesToMTP when they carry this account's id.
	for (auto &entity : parsed) {
		if (entity.type() == EntityType::MentionName) {
			auto fields = TextUtilities::MentionNameDataToFields(entity.data());
			fields.selfId = session->userId().bare;
			entity = EntityInText(
				entity.type(),
				entity.offset(),
				entity.length(),
				TextUtilities::MentionNameDataFromFields(fields));
		}
	}
	const auto mtp = Api::EntitiesToMTP(session, parsed, Api::ConvertOption::WithLocal);
	auto buffer = mtpBuffer();
	mtp.write(buffer);
	const auto from = reinterpret_cast<const char*>(buffer.data());
	return std::vector<char>(from, from + buffer.size() * sizeof(mtpPrime));
}

void FillFromSnapshot(
		not_null<Main::Session*> session,
		const Snapshot &snapshot,
		AyuMessageBase &out) {
	out.userId = UserIdOf(session);
	out.dialogId = snapshot.dialog;
	out.peerId = snapshot.dialog < 0 ? -snapshot.dialog : snapshot.dialog;
	out.messageId = snapshot.msgId;
	out.fromId = snapshot.fromId;
	out.date = snapshot.date;
	out.editDate = snapshot.editDate;
	out.topicId = snapshot.topic;
	out.groupedId = snapshot.groupedId;
	out.replyMessageId = snapshot.replyTo;
	out.text = snapshot.text.toStdString();
	out.textEntities = SerializeEntities(session, snapshot.entities);
	out.mediaPath = "/";
	out.documentType = 0;
	if (snapshot.media) {
		out.mimeType = snapshot.media->mime.toStdString();
		if (out.text.empty()) {
			// Media files are not synced to the desktop yet: keep at least what it was.
			out.text = (u"["_q + snapshot.media->kind + u"]"_q).toStdString();
		}
	}
}

[[nodiscard]] PeerId PeerOfDialog(not_null<Main::Session*> session, ID dialog) {
	if (dialog > 0) {
		return peerFromUser(UserId(BareId(dialog)));
	}
	const auto bare = BareId(-dialog);
	if (session->data().channelLoaded(ChannelId(bare))) {
		return peerFromChannel(ChannelId(bare));
	}
	return peerFromChat(ChatId(bare));
}

// ---- outgoing ----

void Tick();

base::Timer &LoopTimer() {
	static base::Timer timer([] { Tick(); });
	return timer;
}

void WakeUp() {
	base::call_delayed(300, [] { Tick(); });
}

void Enqueue(ID userId, const Event &event) {
	const auto state = ActiveState(userId);
	if (!state || !state->enabled || state->ownerPublic.empty()) {
		return;
	}
	auto item = SyncOutboxItem();
	item.userId = userId;
	item.relayKey = RelayKey(event);
	item.blob = ToChars(Crypto::Seal(ToBytes(state->ownerPublic), ToBytes(EncodeEvent(event))));
	item.createdAt = NowSeconds();
	AyuDatabase::addOutbox(item);
	// Own events are never applied back if the relay ever returns them.
	for (const auto &key : DedupKeys(event)) {
		AyuDatabase::markSeen(userId, key, NowSeconds());
	}
	WakeUp();
}

void FlushDeletes(ID userId, ID dialog) {
	const auto i = PendingDeletes.find({ userId, dialog });
	if (i == end(PendingDeletes)) {
		return;
	}
	auto pending = std::move(i->second);
	PendingDeletes.erase(i);
	auto event = Event();
	event.type = EventType::Delete;
	event.account = int64(userId);
	event.dialog = int64(pending.dialog);
	event.msgIds = std::move(pending.msgIds);
	event.snapshots = std::move(pending.snapshots);
	event.date = NowSeconds();
	Enqueue(userId, event);
}

// ---- loop ----

void Finish(ID userId, const QString &error) {
	auto &loop = Loops[userId];
	loop.busy = false;
	const auto now = crl::now();
	if (error.isEmpty()) {
		loop.backoff = 0;
		loop.nextAttempt = 0;
		loop.lastError = QString();
		if (auto state = AyuDatabase::getSyncState(userId)) {
			state->lastSyncAt = NowSeconds();
			AyuDatabase::saveSyncState(*state);
		}
		return;
	}
	loop.backoff = loop.backoff ? std::min(loop.backoff * 2, kMaxBackoffMs) : kTickMs;
	loop.nextAttempt = now + loop.backoff;
	loop.lastError = error;
	LOG(("TypendruckerGram Sync: %1, retry in %2 s").arg(error).arg(loop.backoff / 1000));
	if (error == u"unauthorized"_q || error == u"http_401"_q) {
		// This device was removed from another one.
		if (auto state = AyuDatabase::getSyncState(userId)) {
			state->enabled = false;
			AyuDatabase::saveSyncState(*state);
		}
	}
}

void ApplyEvent(not_null<Main::Session*> session, const Event &event) {
	const auto userId = UserIdOf(session);
	const auto keys = DedupKeys(event);
	const auto now = NowSeconds();
	switch (event.type) {
	case EventType::Edit: {
		if (keys.empty() || AyuDatabase::isSeen(userId, keys.front())) {
			return;
		}
		const auto &previous = *event.previous;
		if (!AyuDatabase::hasRevision(userId, previous.dialog, previous.msgId, previous.editDate)) {
			auto message = EditedMessage();
			FillFromSnapshot(session, previous, message);
			message.entityCreateDate = event.newEditDate ? event.newEditDate : now;
			AyuDatabase::addEditedMessage(message);
		}
		AyuDatabase::markSeen(userId, keys.front(), now);
	} break;
	case EventType::Delete: {
		const auto ids = std::set<int32>(event.msgIds.begin(), event.msgIds.end());
		auto index = 0;
		for (const auto id : ids) {
			const auto &key = keys[index++];
			if (AyuDatabase::isSeen(userId, key)) {
				continue;
			}
			const auto snapshot = ranges::find(event.snapshots, id, &Snapshot::msgId);
			const auto dialog = (snapshot != end(event.snapshots))
				? snapshot->dialog
				: ID(event.dialog);
			const auto item = dialog
				? session->data().message(PeerOfDialog(session, dialog), MsgId(id))
				: nullptr;
			if (item && !item->isDeleted()) {
				// Loaded here: save it the usual way and mark it in the chat.
				if (!AyuDatabase::hasDeletedMessage(userId, dialog, id)) {
					AyuMessages::addDeletedMessage(item);
				}
				item->setDeleted();
				session->data().requestItemResize(item);
			} else if (snapshot != end(event.snapshots)
				&& !AyuDatabase::hasDeletedMessage(userId, snapshot->dialog, id)) {
				auto message = DeletedMessage();
				FillFromSnapshot(session, *snapshot, message);
				message.entityCreateDate = event.date ? event.date : now;
				AyuDatabase::addDeletedMessage(message);
			}
			AyuDatabase::markSeen(userId, key, now);
		}
	} break;
	case EventType::Message:
		// Caught while every device was offline; saving those is not wired yet.
		for (const auto &key : keys) {
			AyuDatabase::markSeen(userId, key, now);
		}
		break;
	}
}

void Pull(ID userId, Fn<void(QString)> done) {
	const auto state = ActiveState(userId);
	if (!state) {
		done(QString());
		return;
	}
	const auto path = u"/v1/events?limit=%1"_q.arg(kPageSize);
	JsonRequest("GET", state->relayUrl, path, state->token, {}, [=](int status, QJsonObject object) {
		if (!Ok(status)) {
			done(ErrorOf(status, object));
			return;
		}
		const auto session = FindSession(userId);
		const auto ownerPrivate = ToBytes(state->ownerPrivate);
		for (const auto &value : object.value("events").toArray()) {
			const auto item = value.toObject();
			const auto plain = Crypto::Open(ownerPrivate, UnB64(item.value("blob").toString()));
			const auto event = plain ? DecodeEvent(ToQBytes(*plain)) : std::nullopt;
			if (!event) {
				LOG(("TypendruckerGram Sync: cannot read event seq %1"
					).arg(item.value("seq").toVariant().toLongLong()));
				continue;
			}
			if (event->account != int64(userId)) {
				if (const auto other = FindSession(ID(event->account))) {
					ApplyEvent(other, *event);
				}
				continue;
			}
			if (session) {
				ApplyEvent(session, *event);
			}
		}
		if (auto updated = AyuDatabase::getSyncState(userId)) {
			updated->cursor = object.value("cursor").toVariant().toLongLong();
			if (object.value("trimmed").toBool()) {
				updated->trimmed = true;
			}
			AyuDatabase::saveSyncState(*updated);
		}
		if (object.value("more").toBool()) {
			Pull(userId, done);
		} else {
			done(QString());
		}
	});
}

void Flush(ID userId, Fn<void(QString, bool sent)> done, bool sentBefore = false) {
	const auto state = ActiveState(userId);
	auto items = state ? AyuDatabase::getOutbox(userId, kOutboxBatch) : std::vector<SyncOutboxItem>();
	if (items.empty()) {
		done(QString(), sentBefore);
		return;
	}
	auto events = QJsonArray();
	auto ids = std::vector<ID>();
	for (const auto &item : items) {
		ids.push_back(item.id);
		if (!item.blob.empty()) {
			events.push_back(QJsonObject{
				{ "key", QString::fromStdString(item.relayKey) },
				{ "blob", B64(ToBytes(item.blob)) },
			});
		}
	}
	if (events.isEmpty()) {
		AyuDatabase::removeOutbox(ids);
		Flush(userId, done, sentBefore);
		return;
	}
	JsonRequest("POST", state->relayUrl, u"/v1/events"_q, state->token, QJsonObject{ { "events", events } }, [=](int status, QJsonObject object) {
		if (!Ok(status)) {
			done(ErrorOf(status, object), sentBefore);
			return;
		}
		AyuDatabase::removeOutbox(ids);
		Flush(userId, done, true);
	});
}

void RunCycle(ID userId) {
	auto &loop = Loops[userId];
	loop.busy = true;
	Flush(userId, [=](QString error, bool sent) {
		if (!error.isEmpty()) {
			Finish(userId, error);
			return;
		}
		auto &loop = Loops[userId];
		const auto now = crl::now();
		if (now - loop.lastPull >= kPullEveryMs) {
			Pull(userId, [=](QString error) {
				auto &loop = Loops[userId];
				if (error.isEmpty()) {
					loop.lastPull = loop.lastHeartbeat = crl::now();
				}
				Finish(userId, error);
			});
		} else if (!sent && now - loop.lastHeartbeat >= kHeartbeatEveryMs) {
			const auto state = ActiveState(userId);
			if (!state) {
				Finish(userId, QString());
				return;
			}
			JsonRequest("POST", state->relayUrl, u"/v1/heartbeat"_q, state->token, {}, [=](int status, QJsonObject object) {
				if (Ok(status)) {
					Loops[userId].lastHeartbeat = crl::now();
				}
				Finish(userId, Ok(status) ? QString() : ErrorOf(status, object));
			});
		} else {
			Finish(userId, QString());
		}
	});
}

void Tick() {
	if (!Core::IsAppLaunched() || !Core::App().domain().started() || Core::Quitting()) {
		return;
	}
	const auto now = crl::now();
	for (const auto &[index, account] : Core::App().domain().accounts()) {
		const auto session = account->maybeSession();
		if (!session) {
			continue;
		}
		const auto userId = UserIdOf(session);
		const auto state = ActiveState(userId);
		if (!state || !state->enabled) {
			continue;
		}
		auto &loop = Loops[userId];
		if (loop.busy || now < loop.nextAttempt) {
			continue;
		}
		RunCycle(userId);
	}
}

void SaveCredentials(
		ID userId,
		const std::string &relayUrl,
		const QJsonObject &credentials,
		const Bytes &ownerPrivate) {
	auto state = SyncState();
	state.userId = userId;
	state.enabled = true;
	state.relayUrl = relayUrl;
	state.ownerId = credentials.value("ownerId").toString().toStdString();
	state.deviceId = credentials.value("deviceId").toString().toStdString();
	state.token = credentials.value("token").toString().toStdString();
	state.ownerPrivate = ToChars(ownerPrivate);
	state.ownerPublic = ToChars(Crypto::PublicFromPrivate(ownerPrivate));
	if (const auto old = AyuDatabase::getSyncState(userId)) {
		state.mediaLimit = old->mediaLimit;
	}
	AyuDatabase::saveSyncState(state);
	Loops[userId] = UserLoop();
}

} // namespace

void Start() {
	LoopTimer().callEach(kTickMs);
}

void OnEdited(not_null<HistoryItem*> item, TimeId newEditDate) {
	const auto userId = UserIdOf(&item->history()->session());
	const auto state = ActiveState(userId);
	if (!state || !state->enabled) {
		return;
	}
	auto event = Event();
	event.type = EventType::Edit;
	event.account = int64(userId);
	event.previous = SnapshotOf(item);
	event.newEditDate = int32(newEditDate ? newEditDate : NowSeconds());
	Enqueue(userId, event);
}

void OnDeleted(not_null<HistoryItem*> item) {
	const auto userId = UserIdOf(&item->history()->session());
	const auto state = ActiveState(userId);
	if (!state || !state->enabled) {
		return;
	}
	const auto peer = item->history()->peer;
	const auto dialog = peer->isChannel() ? getDialogIdFromPeer(peer) : ID(0);
	const auto key = std::make_pair(userId, dialog);
	auto &pending = PendingDeletes[key];
	if (pending.msgIds.empty()) {
		pending.dialog = dialog;
		base::call_delayed(kDeleteBatchMs, [=] { FlushDeletes(userId, dialog); });
	}
	pending.msgIds.push_back(int32(item->id.bare));
	pending.snapshots.push_back(SnapshotOf(item));
}

Status GetStatus(not_null<Main::Session*> session) {
	const auto userId = UserIdOf(session);
	auto result = Status();
	if (const auto state = ActiveState(userId)) {
		result.paired = true;
		result.enabled = state->enabled;
		result.lastSyncAt = state->lastSyncAt;
		result.trimmed = state->trimmed;
		result.mediaLimit = state->mediaLimit;
	}
	const auto i = Loops.find(userId);
	if (i != end(Loops)) {
		result.lastError = i->second.lastError;
	}
	return result;
}

void SetEnabled(not_null<Main::Session*> session, bool enabled) {
	if (auto state = AyuDatabase::getSyncState(UserIdOf(session))) {
		state->enabled = enabled;
		AyuDatabase::saveSyncState(*state);
	}
	SyncNow();
}

void SetMediaLimit(not_null<Main::Session*> session, int64 limit) {
	if (auto state = AyuDatabase::getSyncState(UserIdOf(session))) {
		state->mediaLimit = std::clamp(limit, int64(0), int64(50) * 1024 * 1024);
		AyuDatabase::saveSyncState(*state);
	}
}

void SyncNow() {
	for (auto &[userId, loop] : Loops) {
		loop.lastPull = 0;
		loop.nextAttempt = 0;
	}
	WakeUp();
}

void CreateOwner(
		not_null<Main::Session*> session,
		const QString &invite,
		Fn<void(QString error)> done) {
	const auto userId = UserIdOf(session);
	const auto keys = Crypto::GenerateKeyPair();
	const auto relay = std::string(kDefaultRelayUrl);
	const auto body = QJsonObject{
		{ "publicKey", B64(keys.publicKey) },
		{ "name", DeviceName() },
		{ "invite", invite.trimmed() },
	};
	JsonRequest("POST", relay, u"/v1/owners"_q, {}, body, [=](int status, QJsonObject object) {
		if (!Ok(status)) {
			done(ErrorOf(status, object));
			return;
		}
		SaveCredentials(userId, relay, object, keys.privateKey);
		SyncNow();
		done(QString());
	});
}

void Join(
		not_null<Main::Session*> session,
		const QString &code,
		Fn<void(QString error)> done) {
	const auto userId = UserIdOf(session);
	const auto normalized = Crypto::NormalizePairCode(code.toStdString());
	if (!normalized) {
		done(u"pairing_not_found"_q);
		return;
	}
	const auto relay = std::string(kDefaultRelayUrl);
	const auto body = QJsonObject{
		{ "lookupHash", QString::fromStdString(Crypto::PairLookupHash(*normalized)) },
		{ "name", DeviceName() },
	};
	JsonRequest("POST", relay, u"/v1/pair/claim"_q, {}, body, [=](int status, QJsonObject object) {
		if (!Ok(status)) {
			done(ErrorOf(status, object));
			return;
		}
		const auto payload = Crypto::PairDecrypt(*normalized, UnB64(object.value("payload").toString()));
		const auto parsed = payload
			? QJsonDocument::fromJson(ToQBytes(*payload)).object()
			: QJsonObject();
		const auto ownerPrivate = UnB64(parsed.value("ownerPrivate").toString());
		if (ownerPrivate.size() != Crypto::kKeySize) {
			done(u"pairing_not_found"_q);
			return;
		}
		const auto url = parsed.value("relayUrl").toString();
		SaveCredentials(
			userId,
			url.isEmpty() ? relay : url.toStdString(),
			object,
			ownerPrivate);
		SyncNow();
		done(QString());
	});
}

void StartPairing(
		not_null<Main::Session*> session,
		Fn<void(QString code, QString error)> done) {
	const auto state = ActiveState(UserIdOf(session));
	if (!state) {
		done(QString(), u"not_paired"_q);
		return;
	}
	const auto code = Crypto::NewPairCode();
	const auto normalized = *Crypto::NormalizePairCode(code);
	const auto payload = QJsonDocument(QJsonObject{
		{ "ownerPrivate", B64(ToBytes(state->ownerPrivate)) },
		{ "relayUrl", QString::fromStdString(state->relayUrl) },
	}).toJson(QJsonDocument::Compact);
	const auto body = QJsonObject{
		{ "lookupHash", QString::fromStdString(Crypto::PairLookupHash(normalized)) },
		{ "kind", u"device"_q },
		{ "payload", B64(Crypto::PairEncrypt(normalized, ToBytes(payload))) },
	};
	JsonRequest("POST", state->relayUrl, u"/v1/pair/start"_q, state->token, body, [=](int status, QJsonObject object) {
		if (!Ok(status)) {
			done(QString(), ErrorOf(status, object));
			return;
		}
		done(QString::fromStdString(code), QString());
	});
}

void Disconnect(not_null<Main::Session*> session, Fn<void()> done) {
	const auto userId = UserIdOf(session);
	const auto state = AyuDatabase::getSyncState(userId);
	const auto forget = [=] {
		auto empty = SyncState();
		empty.userId = userId;
		if (state) {
			empty.mediaLimit = state->mediaLimit;
		}
		AyuDatabase::saveSyncState(empty);
		Loops.erase(userId);
		done();
	};
	if (!state || state->token.empty() || state->deviceId.empty()) {
		forget();
		return;
	}
	const auto path = u"/v1/devices/"_q + QString::fromStdString(state->deviceId);
	JsonRequest("DELETE", state->relayUrl, path, state->token, {}, [=](int, QJsonObject) {
		// Offline or already removed: forget the device locally anyway.
		forget();
	});
}

} // namespace AyuSync
