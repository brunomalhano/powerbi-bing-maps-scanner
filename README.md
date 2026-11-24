# Power BI Bing Maps Inventory Scanner

Scanner de **leitura**, para inventariar visuais internos `map`, `filledMap` e `azureMap` em todos os relatórios do Power BI acessíveis à identidade configurada. Não altera visuais nem publica relatórios.

## Pré-requisitos

1. Um registro de aplicativo no Microsoft Entra ID e credenciais de aplicativo (para produção, prefira certificado/identidade gerenciada).
2. Permitir esse service principal (ou o grupo de segurança que o contém) nas **admin APIs de leitura** no portal de administração do Fabric/Power BI, conforme diretrizes da Microsoft. Em modo service principal, **não configurar permissões Power BI de API que exigem consentimento de administrador** no registro do aplicativo, pois isso pode bloquear as Admin APIs.
3. A API Fabric **Get Report Definition exige leitura e escrita no relatório**, mesmo para operações de leitura. Atribuir a identidade como **Contributor, Member ou Admin** nos workspaces-alvo, conforme política de acesso da empresa, e habilitar uso das Fabric APIs pelo principal. Ser Fabric admin para descobrir workspaces não basta para inspecionar os relatórios.
4. O mesmo principal precisa estar habilitado a acessar a **Get Activity Events** (ou executar `--skip-activity`). Regras de uso dos logs: dados pessoais, acesso restrito e descarte/retencao conforme política corporativa.
5. Python 3.10+ com internet para os endpoints API e as bibliotecas `msal` e `requests`.

**Atenção:** o inventário administrativo pode retornar relatórios para os quais a identidade não tem acesso à definição. Esses relatórios ficam com `inspection_status=ERROR`, e **não** com `SEM_MAPAS_IDENTIFICADOS`.

## Como executar

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export TENANT_ID='SEU-TENANT-ID'
export CLIENT_ID='SEU-APP-ID'
export CLIENT_SECRET='SEU-SECRET'
python scanner.py --days 28 --out ./output
```

Para rodar apenas inventário, sem dados pessoais:

```bash
python scanner.py --skip-activity --out ./output
```

Opcional: `--include-personal` inclui workspaces pessoais (se a API de definição permitir a leitura). Por padrão, excluídos.

## Resultados

- `reports.csv`: todos os relatórios inventariados, com status de cobertura, erro, contagens e links.
- `visuals.csv`: cada visual Bing ou Azure identificado, com página e ID do visual.
- `users.csv`: usuário + relatório a migrar + acessos na janela (somente se auditoria habilitada).
- `summary.json`: indicadores consolidados e total não inspecionado.

**Os relatórios sem acesso** podem estar usando Bing Maps; a cobertura precisa ser medida por `reports_inspected/reports_in_inventory`.

## Interpretação

- `MIGRAR`: foi encontrado ao menos um `map` ou `filledMap` no relatório.
- `JA_USA_AZURE_MAPS`: foi encontrado `azureMap` e nenhum visual Bing no relatório.
- `SEM_MAPAS_IDENTIFICADOS`: definição foi lida, mas sem nenhum dos 3 tipos.
- `NAO_INSPECIONADO`: falha de permissão, limite, sensibilidade, formato ou erro de leitura.
- `unique_viewers_period`: pessoas distintas que abriram o **relatório**, não o visual.
- Usuários que visualizaram por Apps podem gerar IDs distintos em situações específicas; valide a correlação antes de tratar como cobertura integral.
- Os endpoints de auditoria só permitem **até 28 dias de histórico** por consulta, em janelas de um mesmo dia UTC e com paginação. Para análise de 90 dias, será preciso coletar continuamente e armazenar historicamente em local aprovado.

## Próximos passos de migração

1. Priorizar por número de usuários distintos e visualizações dos relatórios, e criticidade declarada.
2. Abrir o relatório no Power BI Desktop e usar **Upgrade maps** ou converter visual individual, disponível para Map e Filled Map.
3. Validar geocodificação, camadas, tamanho de marcadores, mapas preenchidos, filtro/seleções e configurações de rede e privacidade do Azure Maps.
4. Publicar em janela controlada e executar o scanner novamente para evidenciar `azureMap` sem legado.

Referências oficiais:
- https://learn.microsoft.com/en-us/fabric/governance/metadata-scanning-run
- https://learn.microsoft.com/en-us/rest/api/fabric/report/items/get-report-definition
- https://learn.microsoft.com/en-us/rest/api/fabric/articles/item-management/definitions/report-definition
- https://learn.microsoft.com/en-us/rest/api/power-bi/admin/get-activity-events
- https://learn.microsoft.com/en-us/azure/azure-maps/power-bi-visual-conversion
