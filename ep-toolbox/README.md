# EP Toolbox

Preparação local de manuais e QRH: análise do PDF do fabricante, cálculo da revisão,
ROA, ficheiro TAP, incremental, PRINT, Filing Instructions, carta ANAC e Transmittal Letter.

Funciona em **Windows** e **macOS** sem privilégios de administrador. A interface abre no
navegador, numa página que só escuta em `127.0.0.1`. Nenhum documento sai do computador.

## Requisitos

- Python 3.11 ou superior (testado com Python 3.13). Pode ser instalado sem administrador:
  - Windows: instalador do python.org com “Install for me only”, ou Python da Microsoft Store.
  - macOS: Python do python.org (instalação para o utilizador) ou `pyenv`.
- Para exportar Word → PDF, uma destas opções:
  - **Microsoft Word** (só Windows, se já estiver instalado).
  - **LibreOffice**, instalado para o utilizador (`%LOCALAPPDATA%\Programs\LibreOffice` no Windows
    ou `/Applications/LibreOffice.app` no macOS). A aplicação deteta as duas.

## Executar a partir do código

```bash
python -m pip install --user -r requirements.txt
python -m ep_toolbox              # abre o navegador
python -m ep_toolbox --no-browser # só mostra o endereço no terminal
```

## Gerar o executável (Windows ou macOS)

A build tem de ser feita em cada sistema (PyInstaller não faz compilação cruzada):

```bash
python -m pip install --user -r requirements.txt -r requirements-build.txt
python scripts/build.py
```

O resultado é a pasta `dist/EP Toolbox/`. Copia-a para onde quiseres: não precisa de instalação
nem de administrador. Os modelos da empresa podem ser colocados na pasta `models/` que fica
ao lado do executável.

## Onde ficam os dados

| Sistema | Pasta de dados (perfis, capas, edições, cópias de segurança) |
|---|---|
| Windows | `%LOCALAPPDATA%\EP Toolbox` |
| macOS | `~/Library/Application Support/EP Toolbox` |

Pode ser alterada com `EP_TOOLBOX_DATA=/caminho` ou `--data /caminho`.

## Modelos da empresa (não incluídos no repositório)

A aplicação precisa de três modelos Word e de capas TAP em PDF. Não se versionam no repositório:

| Ficheiro | Para quê |
|---|---|
| `roa_template.docx` | ROA (uma linha por revisão) |
| `carta_base.docx` | Carta ANAC |
| `transmittal_template.docx` | Transmittal Letter (tabela Revised Chapter / Description) |

Procuram-se em `models/` (ou na pasta indicada por `EP_TOOLBOX_MODELS`). As capas TAP de cada perfil
(PDF de 4 páginas) importam-se no separador **Perfis e capas**.

## Utilização

1. **Perfis e capas**: importa as capas TAP e, se necessário, o ROA em Word.
2. **Nova edição**: escolhe o perfil e o PDF (ou ZIP, para QRH) → **Analisar**.
3. Revê a análise (data, revisão, páginas SOH/TRL/FI, avisos) e preenche os highlights.
   Podes colar duas colunas do Excel.
4. Marca a revisão e clica em **Gerar documentos**. A revisão só é acrescentada ao histórico
   depois de todos os ficheiros estarem gerados, convertidos e arquivados.
5. Em caso de falha, os documentos ficam numa pasta `__FALHA` recuperável, sem nova revisão.

Os resultados ficam em `saidas/` dentro da pasta de dados. Cada edição tem o PDF completo,
o TAP, o incremental, o PRINT (quando há FI), a carta, o ROA, a Transmittal Letter, mapas de
páginas (CSV), um resumo HTML e um arquivo ZIP verificado.

## Migrar da versão 2.x

Os perfis têm o mesmo formato. Para reaproveitar o histórico e as capas, copia para a nova pasta
de dados: `perfis.json`, `formularios.json` e a pasta `capas/`. As edições antigas (`saidas/`) não
são importadas; continuam disponíveis na pasta da versão 2.

## Estrutura do código

```
ep_toolbox/
  common.py        constantes, datas, JSON atómico
  paths.py         pastas de dados e modelos por sistema (sem administrador)
  history.py       ROA: revisão proposta, validação e atualização
  pdf_analysis.py  análise do PDF (datas no rodapé, SOH/TRL/FI, PLP-LESS)
  pdf_compose.py   composição do completo, TAP, PRINT e FI com bookmarks
  office.py        preenchimento dos modelos DOCX (ROA, carta, Transmittal Letter)
  converters.py    DOCX → PDF (Word no Windows ou LibreOffice)
  qrh.py           compilação de ZIP de QRH com subpastas
  modules.py       registo de módulos de trabalho (ponto de extensão)
  pipeline.py      geração de uma edição, com recuperação e arquivo verificado
  store.py         perfis, capas, formulários, edições e cópias de segurança
  server.py        API local com token e interface
  ui/index.html    interface (sem dependências externas)
tests/             testes unitários e de integração (PDF gerados em código)
scripts/build.py   empacotamento com PyInstaller
```

### Acrescentar uma funcionalidade nova

Um **módulo** (`ep_toolbox/modules.py`) indica que perfis aceita e como analisa a origem.
Para um novo tipo de documento, cria uma subclasse de `Module`, implementa `handles` e
`analyze`, e acrescenta-a a `MODULES`. A geração, o histórico, as cópias de segurança e a
interface reutilizam-se. Uma nova ação na interface acrescenta-se como rota em `server.py`
e como secção em `ui/index.html`.

## Testes

```bash
python -m unittest tests.test_core -v
```

Os testes geram os seus próprios PDF e não precisam dos modelos da empresa. Cobrem datas,
histórico, análise, composição, QRH, a falha com recuperação e a segurança do servidor
(token, Host, nomes de ficheiro como `..`).

## Estado e limitações

- Testado em Linux com Python 3.13: testes automáticos, geração completa com os modelos da
  versão 2.x e LibreOffice, e interface num browser real (Chromium) com o fluxo completo.
- **Não testado** no computador de destino: exportação pelo Microsoft Word (Windows), o executável
  Windows e o `.app` macOS. Valida antes de uso operacional.
- macOS não tem exportação pelo Word nesta versão; usa LibreOffice.
- QRH: o compilado é mantido em memória durante a composição; ZIPs muito grandes (vários GB)
  podem exigir muita RAM. Está por validar com um ZIP real.
- Sem ícone nem assinatura de código: o Windows e o macOS podem mostrar avisos de segurança na
  primeira execução de um executável não assinado.
- Os textos de conteúdo, datas fixas do modelo da carta (ex.: a referência de correspondência) e
  o formato dos modelos estão ligados ao modelo atual; uma alteração ao modelo exige rever
  `office.py`.
