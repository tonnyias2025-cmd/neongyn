---
name: caneca-personalizada
description: Gera peças STL personalizadas (contorno + arte em cores separadas, prontas para Bambu Studio) a partir do logo de um cliente, para aplicar nas canecas Print4You (copo de alumínio padrão), com prévia, mockup na caneca, LEIA-ME e zip. Use sempre que o Tonny pedir para adaptar, aplicar ou trocar logo, nome, escudo, emblema ou texto em caneca, copo ou modelo 3MF da Print4You, ou mandar logo de cliente + arquivo de caneca para gerar peças, mesmo que ele não fale "STL" nem "skill".
---

# Caneca personalizada (peças 3D a partir do logo do cliente)

Processo validado com o pedido THOR (ícone nas 2 laterais + nome na frente, texto pequeno em DTF).
Os 100+ modelos Print4You do Tonny usam o MESMO copo de alumínio; muda só a arte de cada time.
Por isso o corpo da caneca é padrão e as peças do time servem de referência de tamanho.

Medidas, convenções do modelo e solução de problemas: `references/caneca-print4you.md`
(leia antes de decidir tamanhos ou se algo sair estranho).

Script único: `scripts/caneca.py` (subcomandos `analisar`, `logo`, `gerar`).
Dependências (instale se faltar): `pip install trimesh manifold3d shapely svgelements matplotlib lxml scikit-image`
e `pdftocairo` (poppler) para PDF.

## 1. Entrada – confira antes de começar
- **Logo do cliente**: ideal SVG ou PDF vetorial (texto em curvas, sem imagem embutida, sem degradê/sombra).
  CDR: peça exportação para PDF/SVG. PNG/JPG só como último recurso – avise que a fidelidade cai.
- **O que vai onde** (layout flexível por pedido): ex. "ícone nas laterais, nome na frente, texto pequeno no DTF".
  Se não vier, proponha o padrão THOR e confirme.
- **Modelo 3MF da caneca** (opcional, mas melhora tamanho e mockup). Sem ele, use as medidas da referência.
- **Cores de filamento** e cor do corpo da caneca (para escolher o contorno com contraste).
- Bico da impressora: padrão 0,4 (Bambu A2L combo do Tonny).

Arquivos enviados pelo chat ficam em `/root/.claude/uploads/...`. Trabalhe numa pasta própria do pedido (scratchpad).
Se vier zip, descompacte numa pasta vazia.

## 2. Analisar o modelo da caneca (se houver 3MF)
```
python3 scripts/caneca.py analisar MODELO.3mf --saida PEDIDO/analise
```
Lista objetos, placas e tamanhos e salva uma imagem por objeto. Os objetos "emblema/peça plana" são as
peças do time = referência de tamanho de cada posição (ex.: escudo 54×50 mm nas laterais). Olhe as imagens
para saber o que cada um é. Anote o objeto do corpo pontilhado para o mockup (no Santos: obj 8, impresso de
cabeça para baixo → `"inverter": true`). Leva ~30 s com modelos de 50+ MB.

## 3. Mapear o logo
```
python3 scripts/caneca.py logo LOGO.pdf --saida PEDIDO/logo
```
Gera `logo_mapa.png` (cada elemento com um número) e uma tabela com cor e região de cada elemento.
**Olhe o mapa** e decida quais ids formam cada peça. Leia os AVISOS (imagem embutida, texto não convertido,
fundo descartado) e repasse ao Tonny os que afetam a qualidade.

Regra para o que vai ao DTF: texto com altura de letra final < ~6 mm, traço < ~0,6 mm ou letra cursiva fina
não fica bom impresso em 3D – sugira DTF, como no THOR.

## 4. Montar o pedido.json e gerar
```json
{
 "cliente": "THOR",
 "logo_elementos": "logo/logo_elementos.pkl",
 "saida": "THOR_caneca",
 "cor_contorno": "preto",
 "observacao_dtf": "EVENTOS E PUBLICIDADE + slogan, abaixo do THOR",
 "mockup": {"modelo_3mf": "/caminho/MODELO.3mf", "objeto_corpo": "8", "inverter": true},
 "pecas": [
  {"nome": "LATERAL_ICONE", "selecao": {"ids": [0, 1, 10]}, "altura": 52, "quantidade": 2,
   "posicao": "laterais", "referencia_mm": [54, 50]},
  {"nome": "FRENTE_THOR", "selecao": {"ids": [9]}, "largura": 62, "quantidade": 1, "posicao": "frente"}
 ]
}
```
```
python3 scripts/caneca.py gerar PEDIDO/pedido.json
```
Campos úteis:
- `selecao`: `{"ids": [...]}` (preferível) ou `{"regiao": [x0,y0,x1,y1]}` em fração do logo (0–1, origem no topo-esquerda), com `excluir_ids` opcional.
- Tamanho: `largura`, `altura` (da arte) ou `caber: [L, A]` (peça inteira, com contorno, cabe na caixa).
- `posicao`: `frente`, `laterais` (gera mockup nos dois lados), `lateral_esq`, `lateral_dir`, `tras`; `altura_centro` em mm para ajustar no mockup.
- `arte_cor_unica: true` junta todas as cores da arte numa só; sem isso, cada cor do logo vira um STL próprio.
- `suavizar` (padrão 0,15 mm): tira dentes e serrilhados do vetor original (comuns em logos vetorizados no Corel) sem deformar a letra. Use 0 para manter o vetor exato; até 0,3 para logos muito serrilhados (arredonda pontas finas).
- Opcionais globais: `borda` (1,2 mm), `espessura_base`/`espessura_arte` (1,0/1,0 mm), `bico` (0,4), `vao_min` (0,5), `raio_caneca` (49), `fechar` (3,0).
- Sem `mockup.modelo_3mf`, o mockup usa um cilindro genérico do mesmo raio.
- `mockup.cor_corpo` (ex.: `"#1c1c1c"` para caneca preta): cor do corpo no mockup – sem isso fica branco e um contorno branco some.
- Se o contorno ficar com ligação fina entre partes, o script já aumenta o fechamento sozinho; para forçar, use `fechar` maior (5–6).

Por que esses padrões: 1+1 mm é a mesma espessura dos emblemas Print4You (molda com soprador igual);
borda de 1,2 mm cria o "selo" que destaca o logo e une as partes soltas numa peça só;
vão mínimo de 0,5 mm evita que o bico 0,4 funda quadradinhos/letras vizinhos.

**Tamanhos (ponto de partida, ajuste ao modelo):** laterais ≈ tamanho do emblema do time (54×50 → arte de 50–52 mm
de altura); frente: nome com 55–65 mm de largura. Acima de ~60 mm de largura a peça plana fica difícil de curvar
(flecha > 10 mm no raio 49 mm) – o script avisa; ofereça reduzir.

**Cor do contorno:** escolha pelo contraste com a arte E com o corpo da caneca (amarelo em caneca branca → contorno
preto). Diga ao Tonny o porquê; trocar é só mudar o filamento, o arquivo é o mesmo.

Saídas: STLs por camada, `PROJETO_<CLIENTE>_caneca.3mf` (projeto Bambu Studio: uma placa por peça, cópias já
na placa, cada peça com partes contorno/arte e filamento atribuído, cores do filamento já no projeto), prévia, mockup,
LEIA-ME e zip. O 3MF é o arquivo principal para o Tonny – é o mesmo formato dos modelos Print4You. Ele copia o perfil de
impressora/filamento do 3MF modelo (`projeto_modelo_3mf` ou `mockup.modelo_3mf`); sem modelo, sai sem perfil.
Se a arte tiver mais cores do que filamentos no modelo, o script avisa – oriente a adicionar filamento no Bambu.

## 5. Conferir antes de entregar
- Abra `PREVIEW_pecas.png` (vermelho = traço mais fino que o bico) e `MOCKUP_caneca.png`. Se algo estiver
  fora (peça no lugar errado, elemento faltando, logo cortado), corrija o pedido e gere de novo.
- Leia os AVISOS do terminal / `relatorio.json`: contorno em vários pedaços, traços finos, flecha grande,
  peça passando a referência.

## 6. Entregar ao Tonny
- Envie o `PROJETO_<CLIENTE>_caneca.3mf`, o `MOCKUP_caneca.png`, o `PREVIEW_pecas.png` e o zip (`<CLIENTE>_caneca_STL.zip`) com SendUserFile.
- Resposta curta, em português, direta: tabela de peças (nome, tamanho, quantidade), decisões tomadas e por quê
  (tamanho, cor do contorno, vãos alargados), riscos (traço fino, curvatura) e o próximo passo (teste físico).
- Não coloque logo nem STL de cliente em repositório git (o repo do site é público).
- Licença: os modelos Print4You foram comprados pelo Tonny; não redistribua os arquivos originais modificados.
  As peças geradas a partir do logo do cliente são trabalho novo.
