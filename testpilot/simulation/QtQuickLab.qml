import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root
    color: "#f3f6f8"
    property string sampleValue: sample.text
    property string modeValue: mode.currentText
    property bool enabledValue: enabled.checked
    property string resultValue: result.text
    property int canvasCount: 0
    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 28
        spacing: 14
        Label { text: "Qt Quick / QML acceptance lab"; font.pixelSize: 24 }
        TextField {
            id: sample
            text: "QML-INITIAL"
            Accessible.name: "QML Sample"
            Layout.fillWidth: true
        }
        ComboBox {
            id: mode
            Accessible.name: "QML Mode"
            model: ["Standard", "Precision", "Rapid"]
            Layout.fillWidth: true
        }
        CheckBox { id: enabled; text: "QML Enabled"; Accessible.name: "QML Enabled" }
        Button {
            text: "Apply QML"
            Accessible.name: "Apply QML"
            onClicked: result.text = sample.text + " | " + mode.currentText + " | " + enabled.checked
        }
        Label {
            id: result; text: "Not applied"
            Accessible.role: Accessible.StaticText
            Accessible.name: text
            Layout.fillWidth: true
        }
        Item {
            id: canvasAction
            Layout.fillWidth: true
            Layout.preferredHeight: 100
            activeFocusOnTab: true
            Accessible.role: Accessible.Button
            Accessible.name: "QML Canvas action"
            Accessible.onPressAction: root.canvasCount++
            Keys.onSpacePressed: root.canvasCount++
            Canvas {
                anchors.fill: parent
                onPaint: {
                    var c = getContext("2d");
                    c.fillStyle = "#14796e"; c.fillRect(0, 0, width, height);
                    c.fillStyle = "white"; c.font = "20px sans-serif";
                    c.fillText("Custom painted canvas action", 20, 55);
                }
            }
            MouseArea { anchors.fill: parent; onClicked: root.canvasCount++ }
        }
        Label { text: "Canvas count: " + root.canvasCount; Accessible.name: text }
        Item { Layout.fillHeight: true }
    }
}
