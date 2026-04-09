QT       += core gui

greaterThan(QT_MAJOR_VERSION, 4): QT += widgets

CONFIG += c++17

TARGET = pyTunning
TEMPLATE = app

SOURCES += \
    main.cpp \
    mainwindow.cpp

HEADERS += \
    hwhandler.h \
    mainwindow.h

FORMS += \
    mainwindow.ui

RESOURCES += \
    resources.qrc

# Deployment
qnx: target.path = /tmp/$${TARGET}/bin
else: unix: target.path = /opt/$${TARGET}/bin
!isEmpty(target.path): INSTALLS += target

# ✅ Link your custom library (ONLY ONCE)
LIBS += -L$$PWD -lhwhandler

# Include path
INCLUDEPATH += $$PWD
DEPENDPATH += $$PWD
