# Caneca Print4You – medidas e convenções

Medido no modelo "CANECA SANTOS BRANCO" (Bambu Studio 3MF, 7 placas). Os demais modelos Print4You usam o
mesmo copo de alumínio, então corpo, raio e áreas devem ser equivalentes; confira com `analisar` se um modelo
parecer diferente.

## Estrutura típica do 3MF (Santos)
| Placa | Objeto | Tamanho (mm) | O que é |
|---|---|---|---|
| 1 | base/anel | Ø99,5 × 20 | anel inferior (com peça negativa) |
| 2 | corpo inferior + alça | 101,6 × 138 × 123,5 | luva ondulada, anel com texto recortado ("SANTOS FUTEBOL CLUBE") |
| 3 | corpo pontilhado | 99,4 × 135,6 × 91,5 | parte de cima, **impressa de cabeça para baixo** (z=0 é a borda de cima da caneca) |
| 4 | 2 emblemas | 54 × 50 × 2 | escudo das laterais: 1 mm base (branco) + 1 mm detalhes (azul) |
| 5 | texto do anel | faixa ~200 mm × 4 | letras "SANTOS FUTEBOL CLUBE" |
| 6 | plaquinha | 45 × 21,8 × 0,9 | "PEIXE" (0,4 + 0,4 mm) |
| 7 | corpo inferior com letras juntas | — | opção de imprimir as letras junto |

Partes negativas: obj 3 (base) parte 2 = rebaixo do aro (manter); obj 6 parte 5 = texto "SANTOS FUTEBOL CLUBE" recortado no anel (remover para anel liso).
Filamentos no modelo: base = filamento 2; corpos = filamento 1.
Anel da base (obj 6): liso de 0 a ~17 mm (onde ia o texto); faixa texturizada em losangos de 17,3 a 24,3 mm; acima disso a luva.
Montagem (para mockup): obj 6 em pé; obj 8 invertido com a base a +32 mm (a alça do obj 6 atravessa o encaixe do obj 8 e termina rente ao topo, 123,5 mm); obj 3 (aro) por cima, de 123,5 a 140,7 mm.

Alça em +Y no arquivo. Frente da caneca = lado oposto à alça (-Y). Laterais = ±X.

## Medidas do corpo
- Raio externo do corpo pontilhado: 46–51 mm (afina para cima). Use **49 mm** como raio de referência.
- Altura útil (corpo pontilhado, a partir da borda de cima): frente ~74 mm, laterais ~89 mm, trás ~59 mm
  (a borda de baixo é ondulada e encontra a luva).
- Posições no mockup (fração da altura do corpo, medida de baixo): frente 0,72; laterais 0,57.

## Peça plana numa superfície curva (raio 49 mm)
Flecha = quanto o centro da peça fica afastado se encostar as pontas.
| Largura da peça | Flecha | Comentário |
|---|---|---|
| 45 mm | 5,5 mm | fácil (tamanho do "PEIXE") |
| 54 mm | 8,1 mm | padrão dos escudos (molda com soprador) |
| 60 mm | 10,3 mm | limite confortável |
| 65 mm | 12,3 mm | difícil, teste antes |

## Bambu Studio (multicor)
1. Importar juntos `*_1_BASE_contorno_*.stl` + `*_N_ARTE_*.stl` da mesma peça.
2. "Carregar como um objeto com várias partes?" → Sim. As partes já estão alinhadas (mesma origem).
3. Atribuir filamento por parte. Perfil dos modelos Print4You: 0,2 mm de camada, 2 paredes.
4. Arachne (padrão) imprime traços finos até ~0,2 mm afinados; abaixo disso somem. Bico 0,2 para logos muito finos.

## Problemas comuns
| Sintoma | Causa provável | Ação |
|---|---|---|
| `logo` mostra 1 elemento gigante / tudo preto | fundo retangular no arquivo | já é descartado se cobrir >90%; senão use `excluir_ids` |
| aviso "imagem embutida" | PDF com bitmap dentro | pedir o vetor de verdade; ou exportar a imagem e usar PNG |
| aviso "texto não convertido" | fonte viva no SVG | converter em curvas (Corel Ctrl+Q) |
| contorno em vários pedaços | partes muito afastadas | aumentar `fechar` (ex.: 6) ou separar em peças diferentes |
| letras finas somem na prévia | arte pequena demais | aumentar a peça ou mandar aquele texto para DTF |
| mockup com corpo deslocado | centro mal detectado | passar `"centro_xy": [x, y]` em `mockup` (coordenadas do 3MF) |
| logo de cabeça para baixo no mockup | corpo não está invertido | alternar `"inverter"` |
