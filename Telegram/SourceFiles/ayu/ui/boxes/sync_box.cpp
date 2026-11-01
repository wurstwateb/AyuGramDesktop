// This is the source code of TypendruckerGram, based on AyuGram for Desktop.
#include "ayu/ui/boxes/sync_box.h"

#include "ayu/sync/sync_manager.h"
#include "lang/lang_keys.h"
#include "main/main_session.h"
#include "settings/settings_common.h"
#include "styles/style_boxes.h"
#include "styles/style_layers.h"
#include "styles/style_settings.h"
#include "styles/style_widgets.h"
#include "ui/boxes/confirm_box.h"
#include "ui/boxes/single_choice_box.h"
#include "ui/layers/generic_box.h"
#include "ui/text/text_utilities.h"
#include "ui/widgets/buttons.h"
#include "ui/widgets/fields/input_field.h"
#include "ui/widgets/labels.h"
#include "ui/wrap/vertical_layout.h"
#include "window/window_session_controller.h"

#include <QtGui/QGuiApplication>
#include <QtGui/QClipboard>

namespace {

constexpr auto kMegabyte = int64(1024 * 1024);
const auto kMediaLimitsMb = std::vector<int64>{ 0, 5, 10, 20, 50 };

[[nodiscard]] QString DescribeError(const QString &code) {
	if (code == u"invite_required"_q) {
		return tr::ayu_SyncBadInvite(tr::now);
	} else if (code == u"pairing_not_found"_q) {
		return tr::ayu_SyncBadCode(tr::now);
	} else if (code == u"rate_limited"_q) {
		return tr::ayu_SyncRateLimited(tr::now);
	}
	return tr::ayu_SyncError(tr::now, lt_error, code);
}

[[nodiscard]] QString MediaLimitText(int64 megabytes) {
	if (!megabytes) {
		return tr::ayu_SyncMediaOff(tr::now);
	}
	const auto text = QString::number(megabytes) + u" MB"_q;
	return (megabytes * kMegabyte == AyuSync::kDefaultMediaLimit)
		? (text + u" · "_q + tr::ayu_SyncRecommended(tr::now))
		: text;
}

[[nodiscard]] QString StatusText(const AyuSync::Status &status) {
	auto result = QString();
	if (!status.lastError.isEmpty()) {
		result = DescribeError(status.lastError);
	} else if (status.lastSyncAt) {
		const auto when = QDateTime::fromSecsSinceEpoch(status.lastSyncAt);
		result = tr::ayu_SyncLast(
			tr::now,
			lt_date,
			QLocale().toString(when, QLocale::ShortFormat));
	} else {
		result = tr::ayu_SyncNever(tr::now);
	}
	if (status.trimmed) {
		result += u"\n"_q + tr::ayu_SyncTrimmed(tr::now);
	}
	return result;
}

void Reopen(not_null<Window::SessionController*> controller) {
	controller->show(Box(SyncBox, controller));
}

void AskText(
		not_null<Window::SessionController*> controller,
		rpl::producer<QString> title,
		rpl::producer<QString> placeholder,
		Fn<void(QString)> done) {
	controller->show(Box([=, title = std::move(title), placeholder = std::move(placeholder)](
			not_null<Ui::GenericBox*> box) mutable {
		box->setTitle(std::move(title));
		const auto field = box->addRow(object_ptr<Ui::InputField>(
			box,
			st::defaultInputField,
			Ui::InputField::Mode::NoNewlines,
			std::move(placeholder)));
		box->setFocusCallback([=] {
			field->setFocusFast();
		});
		const auto submit = [=] {
			const auto text = field->getLastText().trimmed();
			if (text.isEmpty()) {
				field->showError();
				return;
			}
			box->closeBox();
			done(text);
		};
		field->submits(
		) | rpl::on_next([=](Qt::KeyboardModifiers) {
			submit();
		}, box->lifetime());
		box->addButton(tr::lng_box_ok(), submit);
		box->addButton(tr::lng_cancel(), [=] { box->closeBox(); });
	}));
}

} // namespace

void SyncBox(
		not_null<Ui::GenericBox*> box,
		not_null<Window::SessionController*> controller) {
	const auto session = &controller->session();
	const auto status = AyuSync::GetStatus(session);
	const auto content = box->verticalLayout();

	box->setTitle(tr::ayu_SyncTitle());
	box->setWidth(st::boxWideWidth);

	const auto finished = [=](QString error) {
		if (!error.isEmpty()) {
			controller->showToast(DescribeError(error));
		} else {
			controller->showToast(tr::lng_box_done(tr::now));
		}
		Reopen(controller);
	};

	if (!status.paired) {
		box->addRow(object_ptr<Ui::FlatLabel>(
			box,
			tr::ayu_SyncSetupInfo(),
			st::boxLabel));
		Settings::AddButtonWithIcon(
			content,
			tr::ayu_SyncJoin(),
			st::settingsButtonNoIcon
		)->setClickedCallback([=] {
			box->closeBox();
			AskText(controller, tr::ayu_SyncJoin(), rpl::single(u"XXXX-XXXX-XXXX"_q), [=](QString code) {
				AyuSync::Join(session, code, finished);
			});
		});
		Settings::AddButtonWithIcon(
			content,
			tr::ayu_SyncCreate(),
			st::settingsButtonNoIcon
		)->setClickedCallback([=] {
			box->closeBox();
			AskText(controller, tr::ayu_SyncCreate(), tr::ayu_SyncInvite(), [=](QString invite) {
				AyuSync::CreateOwner(session, invite, finished);
			});
		});
		box->addButton(tr::lng_close(), [=] { box->closeBox(); });
		return;
	}

	box->addRow(object_ptr<Ui::FlatLabel>(
		box,
		rpl::single(StatusText(status)),
		st::boxLabel));

	Settings::AddButtonWithIcon(
		content,
		tr::ayu_SyncEnabled(),
		st::settingsButtonNoIcon
	)->toggleOn(rpl::single(status.enabled)
	)->toggledValue(
	) | rpl::skip(1) | rpl::on_next([=](bool enabled) {
		AyuSync::SetEnabled(session, enabled);
	}, box->lifetime());

	Settings::AddButtonWithIcon(
		content,
		tr::ayu_SyncAddDevice(),
		st::settingsButtonNoIcon
	)->setClickedCallback([=] {
		AyuSync::StartPairing(session, [=](QString code, QString error) {
			if (!error.isEmpty()) {
				controller->showToast(DescribeError(error));
				return;
			}
			controller->show(Ui::MakeConfirmBox({
				.text = tr::ayu_SyncCodeText(
					tr::now,
					lt_code,
					Ui::Text::Bold(code),
					tr::marked),
				.confirmed = [=](Fn<void()> &&close) {
					QGuiApplication::clipboard()->setText(code);
					controller->showToast(tr::lng_text_copied(tr::now));
					close();
				},
				.confirmText = tr::lng_context_copy_text(),
				.cancelText = tr::lng_close(),
			}));
		});
	});

	Settings::AddButtonWithLabel(
		content,
		tr::ayu_SyncMediaLimit(),
		rpl::single(MediaLimitText(status.mediaLimit / kMegabyte)),
		st::settingsButtonNoIcon
	)->setClickedCallback([=] {
		auto options = std::vector<QString>();
		auto selected = 0;
		for (auto i = 0; i != int(kMediaLimitsMb.size()); ++i) {
			options.push_back(MediaLimitText(kMediaLimitsMb[i]));
			if (kMediaLimitsMb[i] * kMegabyte == status.mediaLimit) {
				selected = i;
			}
		}
		controller->show(Box([=](not_null<Ui::GenericBox*> choice) {
			SingleChoiceBox(choice, {
				.title = tr::ayu_SyncMediaLimit(),
				.options = options,
				.initialSelection = selected,
				.callback = [=](int index) {
					AyuSync::SetMediaLimit(session, kMediaLimitsMb[index] * kMegabyte);
					Reopen(controller);
				},
			});
		}));
	});

	Settings::AddButtonWithIcon(
		content,
		tr::ayu_SyncNow(),
		st::settingsButtonNoIcon
	)->setClickedCallback([=] {
		AyuSync::SyncNow();
		controller->showToast(tr::ayu_SyncStarted(tr::now));
	});

	Settings::AddButtonWithIcon(
		content,
		tr::ayu_SyncDisconnect(),
		st::settingsAttentionButton
	)->setClickedCallback([=] {
		controller->show(Ui::MakeConfirmBox({
			.text = tr::ayu_SyncDisconnectInfo(),
			.confirmed = [=](Fn<void()> &&close) {
				close();
				AyuSync::Disconnect(session, [=] {
					controller->showToast(tr::ayu_SyncDisconnected(tr::now));
				});
				box->closeBox();
			},
			.confirmText = tr::ayu_SyncDisconnect(),
			.confirmStyle = &st::attentionBoxButton,
		}));
	});

	box->addRow(object_ptr<Ui::FlatLabel>(
		box,
		tr::ayu_SyncMediaInfo(),
		st::boxDividerLabel));

	box->addButton(tr::lng_close(), [=] { box->closeBox(); });
}
