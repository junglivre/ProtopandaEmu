# Protopanda Emulator

<p align="center">
  <img src="https://github.com/mockthebear/proto-panda/raw/main/doc/logoprotopanda.png" alt="Logo do Protopanda">
</p>

[🇺🇸 English](README.md) | **🇧🇷 Português**

Execute o firmware do [Proto-panda](https://github.com/mockthebear/Proto-panda) no computador, sem a placa física.

O Protopanda Emulator está em desenvolvimento para testar animações, conferir a ligação dos painéis e inspecionar o comportamento do firmware antes de levá-lo ao ESP32-S3. A proposta é executar o firmware original, incluindo seus scripts Lua e armazenamento, em vez de recriar a aplicação no computador.

## Hardware de referência

- **ESP32-S3 N16R8:** dois núcleos LX7, 16 MiB de flash e 8 MiB de PSRAM octal.
- **Dois painéis HUB75 de 64×32:** ordem de canais e orientação independentes por painel.
- **OLED SSD1306:** tela de 128×64 via I²C.
- **Cartão SD virtual:** uma pasta local para configurações, scripts Lua e imagens PNG.

## Estado atual

**Desenvolvimento inicial. A execução sem interface gráfica funciona; o emulador desktop ainda não está pronto.**

| Componente | Estado |
| --- | --- |
| Build do firmware | Versão Proto-panda v3.4.0 fixada e verificação dos artefatos. |
| Execução sem interface gráfica | Validação de perfil, UART ao vivo, relatórios de recursos e sessões com tempo limite. |
| Persistência | Flash e SD privado preservados entre parada limpa e reinício. |
| Armazenamento a partir de pasta | Protótipo de leitura verificado; gravação segura de volta à pasta em desenvolvimento. |
| HUB75 e OLED | Existem testes de periféricos; a renderização completa ainda não está disponível. |
| Launcher desktop | Previsto após armazenamento e renderização. |

A execução usa um fork experimental do [QEMU da Espressif](https://github.com/espressif/qemu), com patches em [`backend/qemu/`](backend/qemu/). Os testes dos backends originais ainda encontram bloqueios de boot. Resultados do fork modificado não significam suporte no QEMU original.

## Experimente a execução sem interface gráfica

O desenvolvimento atual usa Linux. São necessários Python 3, `mtools`, o build fixado do firmware e o executável do QEMU experimental. O runner Python usa apenas a biblioteca padrão.

Em um ambiente já preparado, execute a verificação de ponta a ponta:

```bash
python3 scripts/prove-m2.py
```

Ela inicia o firmware real duas vezes, verifica a persistência, rejeita um perfil incompatível e testa o encerramento por tempo limite e interrupção. Cada execução informa o caminho dos logs e relatórios JSON.

O teste usa um conjunto de arquivos SD com o display desabilitado para separar boot e armazenamento da renderização ainda incompleta. Os marcadores de progresso vêm do Lua executado dentro do guest.

### Inicie uma sessão

Salve este conteúdo como `profile.json` na raiz do repositório. Caminhos relativos do firmware são resolvidos a partir da pasta do perfil.

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

A UART aparece no terminal. O relatório JSON registra os hashes do firmware e do perfil, marcadores de boot, métricas disponíveis da memória do guest e o motivo do encerramento. `Ctrl+C` encerra o backend; atingir o tempo limite retorna o código `124`. A rede do guest fica desabilitada.

A API para uso em Python é `protopandaemu.supervisor.run_session(...)`. Chame `SessionHandle.stop()` para uma parada limpa. Flash e SD têm persistências separadas; persistir o SD privado não equivale a sincronizar uma pasta local.

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

## Próximas etapas

1. Concluir o armazenamento a partir de pasta, com gravação segura, backups e recuperação de conflitos.
2. Renderizar HUB75 e OLED a partir das transferências reais dos periféricos do firmware.
3. Adicionar um launcher desktop com configuração por painel, prévias e diagnósticos ao vivo.

Consulte o [`CHANGELOG.md`](CHANGELOG.md) para ver as alterações implementadas.
