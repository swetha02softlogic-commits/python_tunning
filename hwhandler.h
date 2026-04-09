#ifndef HWHANDLER_H
#define HWHANDLER_H

// #include "ltc2614.h"
// #include "vaccum.h"

#include <QThread>
#include <stdint.h>
#include <unistd.h>
#include <iostream>
#include <stdlib.h>
#include <getopt.h>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <linux/types.h>
#include <linux/spi/spidev.h>
#include <fstream>
#include <stdio.h>
#include <stdint.h>
#include <sys/mman.h>
#include <QDebug>
#include <QFile>
#include <QCoreApplication>
#include <QEventLoop>
#include <QTime>
#include <QLineEdit>

#define XPAR_AXI_COMBINED_BASEADDR 	0x43C20000;
//#define XPAR_AXI_COMBINED_BASEADDR 0x43C40000;
#define SPEAKER_BASEADDR		    0x43C30000
#define STEPPER_MOTOR		    0x40000000

#define MAP_SIZE 4096UL
#define MAP_MASK (MAP_SIZE - 1)
#define STEP_REG_OFFSET 0x20        // Frequency control register offset
#define STEP_REG_COUNT 0x24        // Frequency control register offset

#define VIT_ONOFF_REG               24
#define VIT_ONTIME_REG              25
#define VIT_COUNT_REG               26

#define ON_MASK                     0x01
#define OFF_MASK                    0x00

#define VSO_PWM_ON_REG              36
#define VSO_PWM_PERIOD_REG          38
//#define VSO_PWM_ON_REG              40
//#define VSO_PWM_PERIOD_REG          42

#define DIA_ONOFF_REG               20
#define DIA_COUNT_REG               22

#define AI_ONOFF_REG                44
#define AI_PRESET_REG               48
#define AI_COUNT_REG                50
#define PINCH_VALVE                 28
#define SIL_OIL_REG                 0x1C
#define PINCH_COUNT                 0x1E

#define CHANNEL_0                   0x97
#define CHANNEL_1                   0xD7
#define CHANNEL_2                   0xA7
#define CHANNEL_3                   0xE7

#define REG1 32
#define REG2 34
//#define REG2 36


#define PHACO_ONOFF_REG         0
#define FS_COUNT_REG            2
#define PDM_MODE_REG            6
#define PULSE_COUNT_REG         4
#define BURST_LENGTH_REG        8
#define COLD_PULSE_REG          10
#define FREQ_COUNT_REG          12
#define TUNE_REQ_REG            14
#define BURST_OFF_LENGTH_REG    18

#define TUNE_REQUEST_MASK	    0x8000


#define CONTINOUS       0x01
#define PULSE_MODE      0x02
#define OCUPULSE        0x04
#define OCUBURST        0x08
#define SINGLE_BURST    0x05
#define MULTI_BURST     0x06
#define COLD_PHACO      0x03
#define CONTINOUS_BURST 0x06

#define SPEAKER_ASPIRATION  0x81
#define SPEAKER_IRRIGATION  0x82
#define SPEAKER_OCCLUSION   0x84

class hwhandler: public QThread
{
    Q_OBJECT
    int Flow_LUT[42]={95,95,  //0
        110,110,  //2
        112,112,  //4
        115,115,  //6
        122,122,  //8
        130,130, //10
        145,145, //12
        155,150, //14
        165,155, //16
        175,160, //18
        185,170, //20
        195,195, //22
        205,205, //24
        215,215, //26
        225,225, //28
        245,245, //30
        255,255, //32
        265,265, //34
        275,275, //36
        290,290, //38
        299,299  //40
    };
public:
    explicit hwhandler(QObject *parent = 0);
    int memfd;
    static void vit_on(int periodCount);
    static void vit_off();
    static void vit_ontime(int ontime);

    static void vso_off();
    static void vso_ontime(float ontime);
    static void vso_period(float count);
    //vitrectomy

    static void airinjon();
    static void airinjoff();
    static void airpreset(int count);
    static void aircount(int count);
    static void pinchon();
    static void pinchoff();

    static void dia_on();
    static void dia_off();
    void diathermy(int diapow);
    static void dia_count(int count);

    void safetyvent_on();
    void safetyvent_off();
    void pinchvalve_on();
    void pinchvalve_off();
    void pinchvalve2_on();
    void pinchvalve2_off();
    void write_motor(uint16_t status, uint16_t direction, uint16_t value);

    void phaco_on(int nFSCount);
    void phaco_off();
    int phaco_power(int val);
    void pulse_count(int count);
    void pdm_mode(int mode);
    void burst_length(int time);
    void cold_pulse(int time,int pulse);
    void freq_count(int count);
    void burst_off_length(int length);
    void fs_count_limit(int count);
    void digitalgain(int value);
    void emitTuneStartPhaco();
    void emitTuneStopPhaco();


    void convert_dac(int channel, int count);
    void type550X_count(int channel,int count);
    void speaker_on(uint8_t value, uint8_t asp, uint8_t irr, uint8_t occ);
    void speaker_off();
    void vibrator_on(uint8_t onoff,uint8_t position,uint16_t value);
    void vibrator_off();
    void buzz();
    void stepper_motorOn();
    void stepper_motorOFF();
    void stepper_motorCount(int count);
    void stepper_motorclk(int clock);



signals:

private:
    // ltc2614 *l;
    // Vaccum *v;
    QString fscountlimit;
    QString freqcount;


};
#endif // HWHANDLER_H
