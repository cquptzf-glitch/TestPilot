#include "QtFeatures.h"
#include <QApplication>
#include <QCheckBox>
#include <QDateEdit>
#include <QDialog>
#include <QDialogButtonBox>
#include <QFile>
#include <QDir>
#include <QFormLayout>
#include <QHeaderView>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QMenuBar>
#include <QPainter>
#include <QPushButton>
#include <QQuickItem>
#include <QQuickView>
#include <QRadioButton>
#include <QSlider>
#include <QSpinBox>
#include <QTabWidget>
#include <QTableWidget>
#include <QTimer>
#include <QTreeWidget>
#include <QVBoxLayout>

// Custom painting keeps QSlider's accessible value/range/keyboard contract.
class PaintedGauge final : public QSlider {
public:
    explicit PaintedGauge(QWidget *parent) : QSlider(Qt::Horizontal, parent) {
        setRange(0, 100); setValue(25); setMinimumHeight(44);
        setAccessibleName("Custom gauge"); setObjectName("customGauge");
    }
protected:
    void paintEvent(QPaintEvent *) override {
        QPainter p(this); p.setRenderHint(QPainter::Antialiasing);
        p.fillRect(rect(), QColor("#edf3f5"));
        p.fillRect(0, 8, width() * value() / 100, height() - 16, QColor("#14796e"));
        p.setPen(QColor("#152f38")); p.drawText(rect(), Qt::AlignCenter, QString::number(value()));
    }
};

class QtFeatureLab final : public QWidget {
public:
    QtFeatureLab(QWidget *parent, const QString &directory, const QString &id)
        : QWidget(parent, Qt::Window), exports(directory), suffix(id) {
        setAttribute(Qt::WA_DeleteOnClose);
        setWindowTitle("TestPilot Qt Controls Lab - " + suffix);
        setObjectName("qtControlsLab"); resize(900, 720);
        auto *outer = new QVBoxLayout(this);
        auto *menu = new QMenuBar(this);
        auto *tools = menu->addMenu("Lab tools");
        auto *reset = tools->addAction("Reset selections");
        outer->setMenuBar(menu);
        auto *heading = new QLabel("Qt controls acceptance lab", this);
        outer->addWidget(heading);
        tabs = new QTabWidget(this); tabs->setAccessibleName("Feature tabs");
        outer->addWidget(tabs);
        auto *settings = new QWidget(tabs);
        auto *form = new QFormLayout(settings);
        check = new QCheckBox("Enable audit", settings); check->setAccessibleName("Enable audit");
        form->addRow("Audit", check);
        radio = new QRadioButton("Precision profile", settings); radio->setAccessibleName("Precision profile");
        form->addRow("Profile", radio);
        shared = new QLineEdit("MAIN", settings); shared->setAccessibleName("Shared input");
        form->addRow("Window scope", shared);
        spin = new QSpinBox(settings); spin->setRange(1, 999); spin->setValue(5);
        spin->setAccessibleName("Replicate count"); form->addRow("Replicates", spin);
        date = new QDateEdit(QDate(2026, 10, 16), settings);
        date->setDisplayFormat("yyyy-MM-dd"); date->setAccessibleName("Run date");
        form->addRow("Run date", date);
        gauge = new PaintedGauge(settings); form->addRow("Custom painted gauge", gauge);
        stateLabel = new QLabel("Audit disabled", settings); stateLabel->setObjectName("auditState");
        form->addRow("State", stateLabel);
        auto *dialogButton = new QPushButton("Open modal dialog", settings);
        dialogButton->setAccessibleName("Open modal dialog"); form->addRow(dialogButton);
        auto *auxButton = new QPushButton("Open auxiliary window", settings);
        auxButton->setAccessibleName("Open auxiliary window"); form->addRow(auxButton);
        auto *qmlButton = new QPushButton("Open QML window", settings);
        qmlButton->setAccessibleName("Open QML window"); form->addRow(qmlButton);
        confirmation = new QLabel("No confirmation", settings);
        confirmation->setObjectName("confirmationResult"); form->addRow(confirmation);
        tabs->addTab(settings, "Settings");

        table = new QTableWidget(80, 3, tabs);
        table->setAccessibleName("Results table"); table->setObjectName("resultsTable");
        table->setHorizontalHeaderLabels({"Sample", "Value", "Unit"});
        table->horizontalHeader()->setSectionResizeMode(QHeaderView::Stretch);
        table->setSelectionBehavior(QAbstractItemView::SelectItems);
        for (int row = 0; row < 80; ++row) {
            table->setItem(row, 0, new QTableWidgetItem(QString("SAMPLE-%1").arg(row + 1, 3, 10, QChar('0'))));
            table->setItem(row, 1, new QTableWidgetItem(row == 0 ? "42.5" : QString::number(row)));
            table->setItem(row, 2, new QTableWidgetItem("mg/L"));
        }
        tabs->addTab(table, "Results");
        auto *navigation = new QWidget(tabs); auto *nav = new QHBoxLayout(navigation);
        tree = new QTreeWidget(navigation); tree->setHeaderLabel("Projects");
        tree->setAccessibleName("Project tree");
        auto *root = new QTreeWidgetItem(tree, {"Laboratory"});
        new QTreeWidgetItem(root, {"Chemistry"}); new QTreeWidgetItem(root, {"Hematology"});
        root->setExpanded(false); nav->addWidget(tree);
        list = new QListWidget(navigation); list->setAccessibleName("Sample list");
        list->addItems({"Alpha", "Beta", "Gamma"}); nav->addWidget(list);
        tabs->addTab(navigation, "Navigation");

        connect(check, &QCheckBox::toggled, this, [this](bool on) {
            stateLabel->setText(on ? "Audit enabled" : "Audit disabled"); save();
        });
        connect(radio, &QRadioButton::toggled, this, [this] { save(); });
        connect(shared, &QLineEdit::textChanged, this, [this] { save(); });
        connect(spin, &QSpinBox::valueChanged, this, [this] { save(); });
        connect(date, &QDateEdit::dateChanged, this, [this] { save(); });
        connect(gauge, &QSlider::valueChanged, this, [this] { save(); });
        connect(table, &QTableWidget::itemChanged, this, [this] { save(); });
        connect(table, &QTableWidget::currentCellChanged, this, [this] { save(); });
        connect(tree, &QTreeWidget::currentItemChanged, this, [this] { save(); });
        connect(list, &QListWidget::currentTextChanged, this, [this] { save(); });
        connect(reset, &QAction::triggered, this, [this] {
            check->setChecked(false); radio->setAutoExclusive(false); radio->setChecked(false);
            radio->setAutoExclusive(true); gauge->setValue(25); menuResetCount++; save();
        });
        connect(dialogButton, &QPushButton::clicked, this, [this] { openDialog(); });
        connect(auxButton, &QPushButton::clicked, this, [this] { openAuxiliary(); });
        connect(qmlButton, &QPushButton::clicked, this, [this] { openQml(); });
        save();
    }
private:
    QString exports, suffix;
    QTabWidget *tabs = nullptr;
    QCheckBox *check = nullptr;
    QRadioButton *radio = nullptr;
    QLineEdit *shared = nullptr;
    QSpinBox *spin = nullptr;
    QDateEdit *date = nullptr;
    PaintedGauge *gauge = nullptr;
    QLabel *stateLabel = nullptr, *confirmation = nullptr;
    QTableWidget *table = nullptr;
    QTreeWidget *tree = nullptr;
    QListWidget *list = nullptr;
    QQuickView *qml = nullptr;
    int acceptedCount = 0, cancelledCount = 0, menuResetCount = 0;
    QString auxiliaryText;
    void write(const QString &name, const QJsonObject &state) {
        QFile file(QDir(exports).filePath(name));
        if (file.open(QIODevice::WriteOnly))
            file.write(QJsonDocument(state).toJson(QJsonDocument::Indented));
    }
    void save() {
        write("qt-controls-state.json", {
            {"audit", check->isChecked()}, {"precision", radio->isChecked()},
            {"shared_main", shared->text()}, {"replicates", spin->value()},
            {"date", date->date().toString(Qt::ISODate)}, {"gauge", gauge->value()},
            {"table_value", table->item(0, 1)->text()}, {"last_sample", table->item(79, 0)->text()},
            {"selected_row", table->currentRow()}, {"selected_column", table->currentColumn()},
            {"tree", tree->currentItem() ? tree->currentItem()->text(0) : QString()},
            {"list", list->currentItem() ? list->currentItem()->text() : QString()},
            {"confirmation", confirmation->text()}, {"accepted", acceptedCount},
            {"cancelled", cancelledCount}, {"menu_resets", menuResetCount},
            {"shared_auxiliary", auxiliaryText}
        });
    }
    void openDialog() {
        auto *dialog = new QDialog(this); dialog->setAttribute(Qt::WA_DeleteOnClose);
        dialog->setWindowTitle("TestPilot Qt Confirmation - " + suffix);
        dialog->setModal(true); dialog->resize(460, 200);
        auto *layout = new QVBoxLayout(dialog);
        auto *edit = new QLineEdit(dialog); edit->setAccessibleName("Confirmation code");
        layout->addWidget(edit);
        auto *buttons = new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel, dialog);
        buttons->button(QDialogButtonBox::Ok)->setText("Confirm");
        buttons->button(QDialogButtonBox::Ok)->setAccessibleName("Confirm");
        buttons->button(QDialogButtonBox::Cancel)->setAccessibleName("Cancel");
        layout->addWidget(buttons);
        connect(buttons, &QDialogButtonBox::accepted, dialog, &QDialog::accept);
        connect(buttons, &QDialogButtonBox::rejected, dialog, &QDialog::reject);
        connect(dialog, &QDialog::accepted, this, [this, edit] {
            acceptedCount++; confirmation->setText("Confirmed: " + edit->text()); save();
        });
        connect(dialog, &QDialog::rejected, this, [this] { cancelledCount++; save(); });
        dialog->show();
    }
    void openAuxiliary() {
        auto *window = new QWidget(this, Qt::Window); window->setAttribute(Qt::WA_DeleteOnClose);
        window->setWindowTitle("TestPilot Qt Auxiliary - " + suffix); window->resize(460, 220);
        auto *layout = new QVBoxLayout(window);
        auto *edit = new QLineEdit("AUX", window); edit->setAccessibleName("Shared input");
        layout->addWidget(edit);
        auto *close = new QPushButton("Close auxiliary", window);
        close->setAccessibleName("Close auxiliary"); layout->addWidget(close);
        connect(edit, &QLineEdit::textChanged, this, [this](const QString &text) {
            auxiliaryText = text; save();
        });
        connect(close, &QPushButton::clicked, window, &QWidget::close);
        window->show();
    }
    void openQml() {
        if (qml) { qml->show(); qml->requestActivate(); return; }
        qml = new QQuickView;
        qml->setTitle("TestPilot Qt QML - " + suffix);
        qml->setResizeMode(QQuickView::SizeRootObjectToView);
        qml->setSource(QUrl("qrc:/qt-lab/QtQuickLab.qml")); qml->resize(640, 600);
        qml->show();
        auto *timer = new QTimer(this); timer->setInterval(100);
        connect(timer, &QTimer::timeout, this, [this] {
            if (!qml || !qml->rootObject()) return;
            auto *root = qml->rootObject();
            write("qt-qml-state.json", {
                {"sample", root->property("sampleValue").toString()},
                {"mode", root->property("modeValue").toString()},
                {"enabled", root->property("enabledValue").toBool()},
                {"result", root->property("resultValue").toString()},
                {"canvas_count", root->property("canvasCount").toInt()}
            });
        });
        timer->start();
        connect(this, &QObject::destroyed, qml, &QQuickView::deleteLater);
    }
};

void openQtFeatureLab(QWidget *parent, const QString &exports, const QString &suffix) {
    auto *window = new QtFeatureLab(parent, exports, suffix); window->show();
}
