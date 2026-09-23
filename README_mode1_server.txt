Para rodar o servidor local Mode 1 (sem PHP):

1) Garanta que você tem Python 3 instalado.

2) Na pasta "C:\Users\USER\Documents\Nova pasta":
   python server.py

3) Abra no navegador:
   http://localhost:8000/poker_gto_mode1_app.html

4) Gere/garanta os arquivos (modo v2 por OPEN do vilão):
   storage\preflop_gto_defend_CO_vsOpen_UTG.json
   storage\preflop_gto_defend_BTN_vsOpen_CO.json
   storage\preflop_gto_defend_BB_vsOpen_BTN.json

   Observação: se algum arquivo v2 não existir, o server cai no fallback "legacy" (um arquivo por HERO_POS), mas o ajuste de range por open do vilão não será aplicado.

   Após atualizar o solver (3bet interno 7.5/9.0bb), regenere com:
     python mode1_generate_v2.py --force

   A UI continua mostrando só FOLD/CALL/3BET; o 3BET agora é mix agregado dos dois sizes.

O endpoint fica em:
  POST http://localhost:8000/api/query_preflop_gto.php

Observação: isso evita o problema de CORS que acontece quando abre via file://.
