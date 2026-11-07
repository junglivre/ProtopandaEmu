# Protopanda Emulator

<p align="center">
  <img src="https://github.com/mockthebear/proto-panda/raw/main/doc/logoprotopanda.png" alt="Logo do Protopanda">
</p>

[🇺🇸 English](README.md) | **🇧🇷 Português**

Execute o firmware do [Proto-panda](https://github.com/mockthebear/Proto-panda) no seu computador, sem a placa física.

O Protopanda Emulator está sendo construído para testar animações, conferir a fiação dos painéis e inspecionar o comportamento do firmware antes de enviar para um ESP32-S3. O objetivo é rodar o firmware original, incluindo seus scripts Lua e armazenamento, em vez de recriar a aplicação no host.

## Hardware de referência

- **ESP32-S3 N16R8:** dois núcleos LX7, 16 MiB de flash e 8 MiB de PSRAM octal.
- **Dois painéis HUB75 64×32:** ordem de canal e orientação independentes por painel.
- **OLED SSD1306:** display 128×64 via I²C.
- **Cartão SD virtual:** uma pasta local para configurações, scripts Lua e imagens PNG.

## Estado atual

**O fluxo "rode seu próprio fork de firmware e cartão SD como estão"
(veja abaixo) é o caminho principal suportado e está sólido no dia a
dia:** bugs reais do modelo QEMU (travamento de log do I2C, um bug de
decodificador HUB75 que duplicava quadrantes, uma race de SDMMC que
bloqueava todo conteúdo real) estão corrigidos; o launcher desktop é uma
GUI traduzida com tema escuro, prévia em três painéis fiel ao
dispositivo, seletor de firmware, controlador IR virtual e um toggle do
sensor de proximidade (boop). O trilho separado, guiado por
especificação, de marcos M2-M5 (Proto-panda v3.4.0 fixado,
`prove-m2.py`/`prove-m3.py`/`prove-m4-oled.py`) também funciona de ponta
a ponta, mas um comparador formal de quadro-padrão com seis permutações
para o próprio gate de aceitação Rock 3/M4 desse trilho nunca foi
construído — isso não bloqueia o fluxo como-está, que usa o mesmo
decodificador HUB75 já verificado.

| Componente | Estado |
| --- | --- |
| Build do firmware | Versão Proto-panda v3.4.0 fixada e verificação dos artefatos. Uma variante `-DPROTOPANDAEMU_EMU=1` também compila a partir do fork de firmware do próprio usuário, como está (`scripts/build-firmware-emu.sh`), isolada inteiramente atrás dessa flag. |
| Execução sem interface gráfica | Validação de perfil, UART ao vivo, relatórios de recursos e sessões com tempo limite. |
| Persistência | Flash e SD privado preservados entre parada limpa e reinício. |
| Armazenamento a partir de pasta | A pasta escolhida vira o cartão SD do firmware: preparação segura, gravação recuperável a falhas, backups e retenção de conflitos. |
| OLED | Protocolo I2C real do SSD1306, GDDRAM real de 128x64; byte a byte idêntico a um oráculo derivado de forma independente. |
| HUB75 | Decodificador verificado contra tráfego GDMA real (transmissão contínua, descritores multi-plano reais); um bug de descritor sentinela que duplicava quadros densos em quadrantes espelhados está corrigido. Conteúdo animado real é capturado de ponta a ponta (`artifacts/mvp/hub75-real-content-proof/`). Os dados de cor decodificados são sempre em brilho máximo (brilho é um sinal separado de OE/blanking que este decodificador não vê); o launcher escala a prévia pro brilho real configurado separadamente, veja abaixo. |
| Launcher desktop | GUI Tkinter traduzida, com tema escuro: prévia em três painéis (OLED + ambas as metades do HUB75) no aspecto nativo, redimensionamento travado em proporção, controles independentes de fiação esquerda/direita, seletor de firmware (escolha explícita sobrepõe a detecção automática), sessões reais via hostdir, diagnósticos ao vivo, controlador IR virtual, um toggle do sensor de proximidade (boop) para cartões SD com `trigger_mode: "gpio"`, e uma prévia ao vivo opcional da fita de LED (captura de buffer do firmware, veja abaixo). |

**Lacunas conhecidas** (hardware não emulado ou não verificado, não bugs de backend):

- **A entrada de microfone/FFT nunca produz uma amostra real.** Este
  backend só modela o periférico ADC SAR single-shot
  (`backend/qemu/new-files/hw/misc/esp32s3_sens.c`,
  `docs/qemu-adc-spike.md`) que outro subsistema do firmware consulta
  durante a calibração do boot; não existe modelo pro ADC
  contínuo/DMA que a amostragem de microfone do `fft.cpp` exige
  (`adc_continuous_read`). Firmware com `fft.enabled: true` no
  `misc.json` inicializa e roda normalmente (confirmado contra um
  cartão SD real do usuário: linhas de log `[FFT] begin()`/`started`,
  sem travar ou crashar), mas `readAndAnalyse()` simplesmente não faz
  nada a cada `ESP_ERR_TIMEOUT`, sem logar erro — espere que
  comportamento reativo a voz simplesmente nunca dispare, não que
  trave ou falhe.
- **A saída de servo não tem feedback visual.** O firmware escreve um
  duty cycle de PWM real; o launcher não tem como mostrar o ângulo
  resultante.
- **O modo de entrada BLE não é suportado** (sem modelo de rádio
  NimBLE/Bluedroid); só `input.mode: "infrared"` inicializa de forma
  confiável neste backend.

A execução usa um fork experimental do [QEMU da Espressif](https://github.com/espressif/qemu), com patches em [`backend/qemu/`](backend/qemu/). Os testes dos backends originais ainda encontram bloqueios de boot. Resultados do fork modificado não significam suporte no QEMU original.

## Experimente a execução sem interface gráfica

O desenvolvimento atual usa Linux. São necessários Python 3, `mtools`, o build fixado do firmware e o executável do QEMU experimental. O runner Python usa apenas a biblioteca padrão.

Com o ambiente preparado, rode a verificação de ponta a ponta:

```bash
python3 scripts/prove-m2.py
```

Isso inicia o firmware real duas vezes, confere o estado persistido, rejeita um perfil incompatível e exercita a limpeza por timeout e por interrupção. Cada execução imprime o caminho dos seus logs e relatórios JSON.

A verificação usa uma fixture de SD com o display desabilitado para isolar o boot e o armazenamento de uma renderização ainda incompleta. Seus marcadores de progresso vêm do Lua rodando dentro do guest.

### Rode uma sessão

Salve isto como `profile.json` na raiz do repositório. Caminhos de firmware relativos resolvem a partir da pasta do perfil.

```json
{
  "firmware_image": "artifacts/build-normal/release/merged_flash.bin",
  "firmware_sha256": "811774774e8a9dbfd9d7a8278efc03b8bb6cd1a028c18ba68e7c7cee26f04bf3",
  "board_profile": {
    "chip": "esp32s3",
    "cores": 2,
    "cpu_mhz": 240,
    "flash_bytes": 16777216,
    "psram_bytes": 8388608,
    "psram_mode": "octal"
  },
  "log_level": "info"
}
```

```bash
python3 -m protopandaemu run \
  --profile profile.json \
  --report run.report.json \
  --uart-log run.uart.log \
  --timeout 90
```

O UART aparece no terminal. O relatório JSON registra os hashes do firmware e do perfil, marcadores de boot, métricas de memória disponível no guest e o motivo de encerramento. `Ctrl+C` encerra o backend; atingir o tempo limite retorna o código `124`. A rede do guest fica desabilitada.

A API para uso em Python é `protopandaemu.supervisor.run_session(...)`. Chame `SessionHandle.stop()` para uma parada limpa. Flash e o domínio SD a partir de pasta são mantidos separados, como descrito a seguir.

### Aponte o firmware para uma pasta

Adicione `--hostdir` para usar uma pasta real em vez do cartão SD de exemplo gerado. A pasta já precisa conter `hardware.json`, `animation.json` e os ativos Lua/PNG que o firmware lê; consulte o checkout fixado em `.work/upstream-v3.4.0` para o layout original.

```bash
python3 -m protopandaemu run \
  --profile profile.json \
  --hostdir /caminho/para/seus/dados-protopanda \
  --report run.report.json \
  --timeout 90
```

A pasta é preparada em uma imagem privada antes do boot e nunca é passada diretamente ao QEMU. Na parada limpa, as gravações do guest são confirmadas de volta através de um journal durável: arquivos novos e alterados são escritos, copiados em backup e renomeados como uma única unidade recuperável. Um arquivo que o firmware alterou enquanto você também o editava no host nunca é sobrescrito silenciosamente — a versão do guest é mantida em um `<nome>.protopandaemu-conflict-...` separado, e o relatório da sessão lista cada conflito e backup. Se uma execução for interrompida no meio de uma confirmação, a próxima sessão (ou `python3 -m protopandaemu fsck-hostdir /caminho/para/seus/dados-protopanda`) termina ou reverte essa mesma confirmação antes que qualquer outra coisa toque a pasta.

Rode a verificação de ponta a ponta para esse caminho com:

```bash
python3 scripts/prove-m3.py
```

Isso prepara uma pasta real, inicia o firmware, confirma que a própria leitura/gravação Lua do guest chegou ao host, e exercita toda a matriz de erros, conflitos e recuperação de falhas com falhas reais do sistema de arquivos.

### Rode o launcher desktop (M5)

O launcher local usa a mesma API do supervisor em processo que o runner sem interface gráfica. Ele grava os campos reais por quadro `flip_left`, `flip_right`, `color_scheme_left` e `color_scheme_right` no `animation.json` da pasta escolhida, e então inicia e para a sessão real do QEMU experimental.

```bash
python3 -m protopandaemu.launcher --hostdir /caminho/para/seus/dados-protopanda
```

O `--timeout` padrão é 600s: o primeiro boot de uma pasta real compõe o
`frames.bulk` do firmware no FFat (de segundos a ~100s dependendo da
quantidade de assets) antes de seguir o progresso normal do boot via
`--hostdir`; um timeout menor mata a sessão no meio da composição e deixa
um arquivo de bulk parcial para o próximo boot. A composição só acontece
uma vez por arquivo de estado de flash.

A prévia decodifica as últimas linhas `GDDRAM_DUMP` e `FRAME_DUMP` do log
de stderr do relatório da sessão em um layout de três painéis fiel ao
dispositivo físico: OLED centralizado acima do par esquerdo/direito de
HUB75, cada um no aspecto nativo exato. O redimensionamento da janela é
travado na proporção do próprio launcher, então arrastar um canto escala
todos os painéis proporcionalmente em vez de distorcer um eixo só.
**Este backend não tem modelo de rádio BLE**: um cartão SD real com
`keybinds.json` em `input.mode: "BLE"` entra direto em loop de
crash/reinício contra o firmware fixado padrão (confirmado) — o launcher
avisa antes de iniciar se detectar isso, e a solução é trocar o
`input.mode` desse arquivo para `"infrared"` com um `infrared_gpio`
numérico. Um ambiente desktop local é necessário. Em um ambiente Linux
sem interface gráfica, com o Xvfb já extraído no repositório, suba um
display X real primeiro e rode o mesmo comando com `DISPLAY` definido:

```bash
.work/mvp-display/root/usr/bin/Xvfb :99 -screen 0 1400x1000x24 -nolisten tcp &
DISPLAY=:99 python3 -m protopandaemu.launcher --hostdir /caminho/para/seus/dados-protopanda
```

A prova automatizada do M5 aciona os callbacks reais dos widgets Tk e uma sessão real do supervisor:

```bash
DISPLAY=:99 python3 scripts/prove-m5-launcher.py
```

### Rode seu próprio fork de firmware e cartão SD como estão

`scripts/build-firmware-emu.sh` compila o firmware do launcher a partir de
uma árvore de firmware local (padrão `PROTO_PANDA_FIRMWARE_SRC`, pode
trocar por essa variável de ambiente) com `-DPROTOPANDAEMU_EMU=1`,
aplicando `backend/firmware-emu/patches/` a uma cópia descartável — nunca
na sua árvore de origem. Esses patches só tocam no que este backend não
consegue suportar de outra forma: o pino compartilhado de IR/SDMMC usa
`gpio_config()` em vez de `pinMode()` para não desconectar o barramento
SD, `BleManager::begin()` roda sem rádio para que chamadas de
`onEnable()` de drivers que exigem BLE passem sob `input.mode:
pressionamentos de IR virtuais, um segundo comando de console
(`GPIO:<pin>:<0|1>\n`) sobrepõe o valor de leitura de qualquer pino de
entrada digital (usado pelo toggle do sensor de proximidade boop
abaixo), a gravação real de WS2812 via `FastLED.show()` continua
stubada (o modelo de RMT do QEMU completa a transação mas não decodifica
o timing do sinal, então não há fio pra acender). Uma flag de build
opcional (`PROTOPANDAEMU_EMU_LED_DUMP`, desligada por padrão — mesmo
limitada, uma sessão animada longa acumula volume relevante de log no
UART) despeja adicionalmente o buffer de cor real que
`LedStrip::Display()` calcula no console, pra prévia de LED ao vivo
abaixo; defina
`PLATFORMIO_BUILD_FLAGS="-DPROTOPANDAEMU_EMU_LED_DUMP"` antes de rodar
`build-firmware-emu.sh` pra ligar, sem editar nenhum arquivo da árvore
de origem. Uma terceira linha de console, sempre ligada
(`[EMU] HUB75_BRIGHTNESS value=<0-255>`), reporta toda mudança real de
brilho do painel (`Devices::SetMaxBrightness`, direta ou a rampa suave
por quadro), limitada e filtrada por mudança da mesma forma; o launcher
escala a prévia decodificada (sempre em brilho máximo) por esse valor.
Nenhuma configuração de firmware, pino ou dado do SD é editado para
fazer nada disso funcionar — o perfil da placa e as atribuições de GPIO
continuam reais.

```bash
PATH="$HOME/.platformio/penv/bin:$PATH" PROTO_PANDA_FIRMWARE_SRC=/caminho/para/seu/fork \
  bash scripts/build-firmware-emu.sh
python3 -m protopandaemu.launcher --hostdir /caminho/para/seus/dados-protopanda
```

O launcher detecta automaticamente
`artifacts/build-emu/release/merged_flash.bin` quando presente — sem
variáveis de ambiente; uma escolha explícita no botão "Choose
firmware..." sempre sobrepõe a detecção automática. O painel "Simulated
IR controls" envia comandos de IR reais do seu `keybinds.json` pelo UART
da sessão enquanto ela roda. Para cartões SD com `misc.json`'s
`boop.trigger_mode: "gpio"`, um toggle "Boop" ao lado sobrepõe o valor
desse pino da mesma forma (roxo quando próximo, cinza quando longe); não
tem efeito em configs de boop no modo lidar, que não têm pino digital
pra sobrepor. Abaixo da prévia HUB75/OLED, uma linha de fita de LED
renderiza as cores reais que `LedStrip::Display()` calculou (não é um
traço de tráfego de hardware verificado como HUB75/OLED, veja Estado
atual acima) para cartões SD que acionam saída WS2812 — fica vazia a
menos que o firmware tenha sido compilado com
`PROTOPANDAEMU_EMU_LED_DUMP` (veja acima).

### Verifique a saída do OLED

```bash
python3 scripts/prove-m4-oled.py
```

Isso inicia o firmware com sua configuração real de display, captura o tráfego real de comandos e pixels I2C do SSD1306, e confere o quadro do ícone de abertura byte a byte contra um oráculo que este script deriva de forma independente a partir do próprio bitmap do ícone compilado no firmware. Ele encerra a sessão logo após o trabalho do OLED terminar, por design, sem esperar o resto do boot.

## Compilação e inspeção

- [`scripts/build-firmware.sh`](scripts/build-firmware.sh) compila o firmware fixado nas variantes release e debug. Exige PlatformIO Core 6.2.0; as versões das dependências Python estão em [`requirements-build.txt`](requirements-build.txt).
- [`backend/qemu/build-experimental-qemu.sh`](backend/qemu/build-experimental-qemu.sh) compila o backend modificado. Atualmente exige um sysroot de dependências preparado em `.work/qemu-deps`; ainda não há instalador de comando único.
- [`scripts/spike.py`](scripts/spike.py) executa experimentos de backend com tempo limite. [`artifacts/`](artifacts/) contém resultados capturados, logs e hashes.

```bash
bash scripts/build-firmware.sh --out artifacts/build-normal
bash backend/qemu/build-experimental-qemu.sh
```

Para executar testes individuais do backend:

```bash
bash scripts/spike.sh --backend=qemu --mode=boot
bash scripts/spike.sh --backend=qemu --mode=hub75-probe
```

Um teste pode registrar um bloqueio ou `NOT_EXECUTED`; nenhum desses resultados indica que o firmware passou. Imagens de disco, ferramentas baixadas e arquivos temporários ficam nas pastas ignoradas `.work/` e `.rf/`.

## Próximos passos

1. Um comparador formal de quadro-padrão com seis permutações pro
   próprio check de aceitação HUB75 do trilho v3.4.0 fixado nunca foi
   construído; não é necessário pro fluxo como-está, que já captura e
   verifica conteúdo animado real de ponta a ponta.
2. Entrada BLE e ferramentas web/modo-edição seguem fora de escopo —
   não existe modelo de rádio pra este backend.

Consulte o [`CHANGELOG.md`](CHANGELOG.md) para ver as alterações implementadas.
