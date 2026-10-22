#include "QtFeatures.h"
#include <QApplication>
#include <QEvent>
#include <QKeyEvent>
#include <QMouseEvent>
#include <QComboBox>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFont>
#include <QFrame>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QLineEdit>
#include <QMainWindow>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QPushButton>
#include <QStandardPaths>
#include <QTimer>
#include <QUrl>
#include <QVBoxLayout>
#include <functional>

class MockInstrument final : public QMainWindow
{
public:
    MockInstrument(const QString &address, const QString &directory)
        : endpoint(address), exports(directory), network(this)
    {
        QDir().mkpath(exports);
        appEventTrace.setFileName(QDir(exports).filePath("ui-events.jsonl"));
        appEventTrace.open(QIODevice::WriteOnly | QIODevice::Append);
        qApp->installEventFilter(this);
        setWindowTitle("TestPilot Mock Instrument - " + QString::number(QUrl(endpoint).port()));
        setObjectName("MockInstrument");
        resize(940, 650);
        setMinimumSize(820, 610);
        auto *central = new QWidget(this);
        setCentralWidget(central);
        auto *layout = new QVBoxLayout(central);
        layout->setContentsMargins(32, 28, 32, 28);
        layout->setSpacing(18);

        auto *eyebrow = new QLabel("LOCAL ACCEPTANCE LAB  /  C++ & Qt 6", this);
        eyebrow->setObjectName("eyebrow");
        layout->addWidget(eyebrow);
        auto *heading = new QLabel("Mock Instrument", this);
        heading->setObjectName("heading");
        layout->addWidget(heading);
        auto *description = new QLabel(
            "Qt Widgets interface connected to the local device simulator", this);
        description->setObjectName("description");
        layout->addWidget(description);

        auto *group = new QGroupBox("Measurement setup", this);
        auto *row = new QHBoxLayout(group);
        row->setContentsMargins(20, 28, 20, 22);
        row->setSpacing(22);
        auto *sampleColumn = new QVBoxLayout;
        sampleColumn->addWidget(new QLabel("Sample ID", group));
        sample = new QLineEdit("SAMPLE-001", group);
        sample->setObjectName("sampleId");
        sample->setAccessibleName("Sample ID");
        sample->setMinimumWidth(220);
        sampleColumn->addWidget(sample);
        row->addLayout(sampleColumn, 2);
        auto *modeColumn = new QVBoxLayout;
        modeColumn->addWidget(new QLabel("Measurement mode", group));
        mode = new QComboBox(group);
        mode->setObjectName("measurementMode");
        mode->setAccessibleName("Measurement mode");
        mode->addItems({"Standard", "Precision"});
        modeColumn->addWidget(mode);
        row->addLayout(modeColumn, 1);
        auto *buttonColumn = new QVBoxLayout;
        buttonColumn->addStretch();
        measure = new QPushButton("Start measurement", group);
        measure->setObjectName("startMeasurement");
        measure->setAccessibleName("Start measurement");
        measure->setMinimumWidth(210);
        buttonColumn->addWidget(measure);
        row->addLayout(buttonColumn);
        layout->addWidget(group);
        connect(measure, &QPushButton::clicked, this, [this] {
            measure->setEnabled(false);
            post("/instrument/measure",
                 {{"sample_id", sample->text()}, {"mode", mode->currentText()}},
                 [this](const QJsonObject &) { refresh(); });
        });

        auto *card = new QFrame(this);
        card->setObjectName("resultCard");
        auto *resultLayout = new QVBoxLayout(card);
        resultLayout->setContentsMargins(24, 20, 24, 20);
        auto *caption = new QLabel("DEVICE RESULT", card);
        caption->setObjectName("caption");
        resultLayout->addWidget(caption);
        auto *resultRow = new QHBoxLayout;
        status = new QLabel("Ready", card);
        status->setObjectName("resultStatus");
        status->setTextInteractionFlags(Qt::NoTextInteraction);
        resultRow->addWidget(status);
        result = new QLabel("-- mg/L", card);
        result->setObjectName("measurementValue");
        result->setAlignment(Qt::AlignRight | Qt::AlignVCenter);
        resultRow->addWidget(result);
        resultLayout->addLayout(resultRow);
        exportStatus = new QLabel("No report exported", card);
        exportStatus->setObjectName("exportStatus");
        resultLayout->addWidget(exportStatus);
        layout->addWidget(card, 1);

        auto *footer = new QHBoxLayout;
        exportButton = new QPushButton("Export report", this);
        exportButton->setObjectName("exportReport");
        exportButton->setAccessibleName("Export report");
        exportButton->setEnabled(false);
        footer->addWidget(exportButton);
        auto *features = new QPushButton("Qt controls lab", this);
        features->setAccessibleName("Qt controls lab");
        footer->addWidget(features);
        connect(features, &QPushButton::clicked, this, [this] {
            openQtFeatureLab(this, exports, QString::number(QUrl(endpoint).port()));
        });
        connection = new QLabel(this);
        connection->setObjectName("connection");
        connection->setWordWrap(true);
        footer->addWidget(connection, 1);
        layout->addLayout(footer);
        connect(exportButton, &QPushButton::clicked, this, [this] {
            exportButton->setEnabled(false);
            post("/instrument/export", {}, [this](const QJsonObject &body) {
                QDir().mkpath(exports);
                const QString name = "measurement-" +
                    QDateTime::currentDateTime().toString("yyyyMMdd-HHmmss-zzz") + ".json";
                QFile file(QDir(exports).filePath(name));
                if (!file.open(QIODevice::WriteOnly)) {
                    exportStatus->setText("Export failed: " + file.errorString());
                    return;
                }
                const QByteArray data = QJsonDocument(body).toJson(QJsonDocument::Indented);
                if (file.write(data) != data.size()) {
                    exportStatus->setText("Export failed: " + file.errorString());
                    return;
                }
                file.close();
                exportStatus->setText("Report saved: " + name);
            });
        });
        setStyleSheet(
            "QMainWindow{background:#f3f6f8;color:#182c33;}"
            "QWidget{font-family:'Segoe UI';font-size:14px;color:#182c33;}"
            "QLabel#eyebrow{color:#14796e;font-size:12px;font-weight:600;}"
            "QLabel#heading{font-size:32px;font-weight:700;}"
            "QLabel#description,QLabel#connection,QLabel#exportStatus{color:#6a7880;}"
            "QGroupBox{border:1px solid #dbe3e8;border-radius:12px;margin-top:12px;"
            "background:white;font-weight:600;}"
            "QGroupBox::title{subcontrol-origin:margin;left:18px;padding:0 6px;}"
            "QLineEdit,QComboBox{border:1px solid #cbd8de;border-radius:6px;"
            "padding:10px;background:white;min-height:22px;}"
            "QPushButton{border:1px solid #14796e;border-radius:7px;padding:12px 18px;"
            "background:#14796e;color:white;font-weight:600;}"
            "QPushButton:disabled{background:#e2ebe8;color:#82968f;border-color:#d4e1dc;}"
            "QPushButton:hover{background:#10675e;}"
            "QFrame#resultCard{background:white;border:1px solid #dbe3e8;border-radius:12px;}"
            "QLabel#caption{color:#6a7880;font-size:12px;}"
            "QLabel#resultStatus{font-size:30px;font-weight:700;color:#14796e;}"
            "QLabel#measurementValue{font-size:34px;font-weight:700;}");
        timer.setInterval(200);
        connect(&timer, &QTimer::timeout, this, [this] { refresh(); });
        timer.start();
        refresh();
    }

protected:
    bool eventFilter(QObject *target, QEvent *event) override
    {
        const auto type = event->type();
        if (type == QEvent::KeyPress || type == QEvent::KeyRelease ||
            type == QEvent::MouseButtonPress || type == QEvent::MouseButtonRelease ||
            type == QEvent::FocusIn || type == QEvent::FocusOut) {
            const QWidget *focused = QApplication::focusWidget();
            QJsonObject row{{"event", static_cast<int>(type)},
                            {"target", target->objectName()},
                            {"class", target->metaObject()->className()},
                            {"focus", focused ? focused->objectName() : QString()},
                            {"active", isActiveWindow()}};
            if (type == QEvent::KeyPress || type == QEvent::KeyRelease)
                row["key"] = static_cast<QKeyEvent *>(event)->key();
            if (type == QEvent::MouseButtonPress || type == QEvent::MouseButtonRelease) {
                const QPointF point = static_cast<QMouseEvent *>(event)->globalPosition();
                row["x"] = point.x();
                row["y"] = point.y();
            }
            appEventTrace.write(QJsonDocument(row).toJson(QJsonDocument::Compact) + "\n");
            appEventTrace.flush();
        }
        return QMainWindow::eventFilter(target, event);
    }

private:
    QFile appEventTrace;
    QString endpoint, exports;
    QNetworkAccessManager network;
    QTimer timer;
    QLineEdit *sample = nullptr;
    QComboBox *mode = nullptr;
    QLabel *status = nullptr;
    QLabel *result = nullptr;
    QLabel *connection = nullptr;
    QLabel *exportStatus = nullptr;
    QPushButton *measure = nullptr;
    QPushButton *exportButton = nullptr;
    bool polling = false;

    void post(const QString &path, const QJsonObject &body,
              const std::function<void(const QJsonObject &)> &done)
    {
        QNetworkRequest request(QUrl(endpoint + path));
        request.setHeader(QNetworkRequest::ContentTypeHeader, "application/json");
        request.setTransferTimeout(3000);
        QNetworkReply *reply = network.post(request, QJsonDocument(body).toJson(QJsonDocument::Compact));
        connect(reply, &QNetworkReply::finished, this, [this, reply, done] {
            if (reply->error() != QNetworkReply::NoError)
                connection->setText("Request failed: " + reply->errorString());
            else
                done(QJsonDocument::fromJson(reply->readAll()).object());
            reply->deleteLater();
        });
    }

    void refresh()
    {
        if (polling) return;
        polling = true;
        QNetworkRequest request(QUrl(endpoint + "/instrument/state"));
        request.setTransferTimeout(1500);
        QNetworkReply *reply = network.get(request);
        connect(reply, &QNetworkReply::finished, this, [this, reply] {
            polling = false;
            if (reply->error() != QNetworkReply::NoError) {
                connection->setText("Connection unavailable: " + reply->errorString());
            } else {
                const QJsonObject state = QJsonDocument::fromJson(reply->readAll()).object();
                const QString phase = state["status"].toString();
                const QString value = state["value"].toString();
                status->setText(phase);
                result->setText((value.isEmpty() ? "--" : value) + " mg/L");
                measure->setEnabled(phase != "Measuring");
                exportButton->setEnabled(phase == "Completed");
                connection->setText("Connected to " + endpoint + "\nScene: " + state["scene"].toString());
            }
            reply->deleteLater();
        });
    }
};

int main(int argc, char **argv)
{
    QApplication app(argc, argv);
    app.setApplicationName("TestPilot Mock Instrument");
    app.setApplicationVersion("1.0.0");
    const QString endpoint = argc > 1 ? QString::fromLocal8Bit(argv[1]) : "http://127.0.0.1:19380";
    const QString directory = argc > 2 ? QString::fromLocal8Bit(argv[2]) :
        QDir(QStandardPaths::writableLocation(QStandardPaths::TempLocation))
            .filePath("TestPilot-QtMock-exports");
    MockInstrument window(endpoint, directory);
    window.show();
    return app.exec();
}
