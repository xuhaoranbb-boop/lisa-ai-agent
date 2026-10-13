"""
Orange Pi 5 Pro GPIO 引脚参考
=============================

本文件记录 Orange Pi 5 Pro 的 GPIO 引脚映射，
供硬件接线时参考。

注意：Orange Pi 5 Pro 使用 RK3588S 芯片，
GPIO 编号和树莓派不同！使用 BCM 编号方式。

40-pin 扩展头引脚定义（部分常用）：
==================================

物理引脚    BCM编号    功能         用途
─────────────────────────────────────────────
Pin 1                  3.3V         供电
Pin 2                  5V           供电
Pin 3       GPIO1_A0   I2C7_SDA    I2C 数据
Pin 4                  5V           供电
Pin 5       GPIO1_A1   I2C7_SCL    I2C 时钟
Pin 6                  GND          地
Pin 7       GPIO1_B2   SPI4_CS     SPI 片选
Pin 8       GPIO0_PD2  UART1_TX    串口发送
Pin 9                  GND          地
Pin 10      GPIO0_PD3  UART1_RX    串口接收
Pin 11      GPIO1_B7   PWM15       ← 按钮（默认 GPIO27 = BCM编号）
Pin 12      GPIO1_C0   PWM14       蜂鸣器
Pin 13      GPIO5_C0   SPI0_MOSI   ← LED 数据（WS2812B via SPI）
Pin 14                 GND          地
Pin 15      GPIO5_B3   SPI0_MISO   SPI 输入
Pin 16      GPIO5_B7   SPI0_CLK    SPI 时钟
Pin 17                 3.3V         供电
Pin 18      GPIO5_C1   SPI0_CS0    SPI 片选
Pin 19      GPIO1_B1   SPI4_MOSI   SPI 输出
Pin 20                 GND          地
Pin 21      GPIO1_B0   SPI4_MISO   SPI 输入
Pin 22                 GND          地
Pin 23      GPIO1_B3   SPI4_CLK    SPI 时钟
Pin 24      GPIO1_B4   SPI4_CS0    SPI 片选
Pin 25                 GND          地
Pin 26      GPIO1_B5   SPI4_CS1    SPI 片选


推荐接线方案：
=============

1. 录音按钮（PTT）
   ┌──────────────┐
   │ 按钮一端     │──→ Pin 11 (GPIO27 / BCM)
   │ 按钮另一端   │──→ Pin 14 (GND)
   └──────────────┘
   说明：内部上拉，按下接地 = 低电平触发

2. WS2812B LED 灯环
   ┌──────────────┐
   │ VCC (红线)   │──→ Pin 2 (5V) 或外部 5V 电源
   │ GND (白线)   │──→ Pin 6 (GND)
   │ DIN (绿线)   │──→ Pin 13 (GPIO_SPI0_MOSI / SPI)
   └──────────────┘
   说明：通过 SPI MOSI 引脚驱动，时序精准
   注意：如果灯珠超过 12 颗，建议外部供电（5V/2A）

3. 麦克风（USB 或 I2S）
   USB 方案（简单）：
     直接插 USB 口，免驱

   I2S 方案（低延迟）：
     需要 I2S 麦克风模块（如 INMP441）
     SDO → Pin 15 (SPI0_MISO)
     WS  → Pin 12 (PWM14)
     SCK → Pin 16 (SPI0_CLK)
     L/R → GND (左声道)
     VDD → Pin 1 (3.3V)
     GND → Pin 9 (GND)

4. 扬声器
   I2S 方案（推荐，MAX98357A 功放模块）：
     DIN → Pin 19 (SPI4_MOSI)
     LRC → Pin 18 (SPI0_CS0)
     BCLK → Pin 23 (SPI4_CLK)
     VCC → Pin 4 (5V)
     GND → Pin 20 (GND)

   简单方案：
     USB 声卡 + 3.5mm 有源音箱


电源注意事项：
=============
- Orange Pi 5 Pro 需要 5V/4A Type-C PD 电源
- WS2812B 12 颗灯环约需 60mA（全白时 ~720mA 峰值）
- MAX98357A 功放峰值约 1A
- 建议总供电 5V/5A 以上，或 LED/功放单独供电

SPI 说明：
=========
Orange Pi 5 Pro 的 SPI 和树莓派不同：
- 使用 spidev 直接驱动 WS2812B
- 需要 2.4MHz SPI 时钟
- 在 /boot/orangepiEnv.txt 中添加 overlays=spi0 启用
- 安装 spi-tools: sudo apt install spi-tools
"""
