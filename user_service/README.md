# User Service

Aplicação Flask para gerir os utilizadores do sistema de correção de exames.
Implementa as funcionalidades da primeira parte do projeto:

- **SU_1:** listar os utilizadores e os respetivos papéis.
- **SU_2:** adicionar um utilizador através de um formulário.
- **SU_3:** importar vários utilizadores através de um ficheiro CSV.

Um utilizador pode ter vários papéis. Nesta parte do projeto, as páginas são
acessíveis sem autenticação.

## Executar

São necessários Python, Flask e SQLAlchemy. O Werkzeug, utilizado para gerar
os hashes das passwords, é instalado como dependência do Flask.

Na raiz do projeto, em PowerShell, cria um ambiente virtual caso ainda não exista:

```powershell
python -m venv venv
```

Instala as dependências e inicia o serviço:

```powershell
.\venv\Scripts\python.exe -m pip install -r user_service\requirements.txt
.\venv\Scripts\python.exe user_service\su_app.py
```

Abre [http://127.0.0.1:5001](http://127.0.0.1:5001) no navegador.
O serviço é iniciado em modo de desenvolvimento, com `debug=True`.

## Páginas

| Caminho | Métodos | Funcionalidade |
| --- | --- | --- |
| `/` | GET | Página inicial com cartões de ação, total de utilizadores e contagens por papel. |
| `/list_users` | GET | SU_1: listar identificadores, nomes e papéis, por ordem de identificador. |
| `/list_users/sucess` | GET | Listar utilizadores com a confirmação de adição ou importação. |
| `/users/add` | GET, POST | SU_2: apresentar o formulário e adicionar um utilizador. |
| `/users/bulk-add` | GET, POST | SU_3: apresentar o formulário e importar um CSV. |

## Dados dos utilizadores

O identificador é introduzido no formulário ou fornecido no CSV. A aplicação
valida que é um inteiro positivo, único e não superior a `2**63 - 1`.

O nome e a password são obrigatórios e não podem conter apenas espaços.
As passwords são guardadas como hashes, gerados por `generate_password_hash`,
e não são apresentadas na listagem.

Os papéis disponíveis são:

- `Administrator`
- `Professor`
- `Exam invigilator`
- `Student`

No formulário de criação, seleciona um ou mais papéis através das checkboxes.
Os nomes dos papéis no CSV têm de corresponder exatamente aos valores acima.
Papéis repetidos para o mesmo utilizador são considerados apenas uma vez.
Antes de guardar, a aplicação confirma que todos os papéis selecionados existem
na base de dados. Se algum estiver em falta, apresenta uma mensagem
e não cria o utilizador nem guarda parcialmente os seus papéis. 
No CSV, a mesma situação anula toda a importação.

## Importação CSV

O ficheiro deve ter extensão `.csv`, usar codificação **UTF-8** (com ou sem BOM)
e separar as colunas por vírgulas. O cabeçalho deve conter as colunas
`id`, `name`, `password` e `role`, sem nomes de colunas repetidos.

Para atribuir vários papéis, separa-os com `|` na coluna `role`.
Campos que contenham vírgulas devem estar entre aspas.

Exemplo:

```csv
id,name,password,role
1001,Ana Silva,testeAna123,Student
2001,João Pereira,testeJoao123,Professor|Exam invigilator
3001,"Costa, Maria",testeMaria123,Exam invigilator
4001,Pedro Santos,testePedro123,Administrator
```

Para testar:

1. Guarda o exemplo num ficheiro `.csv` com codificação UTF-8.
2. Abre a página **Bulk Add Users**.
3. Seleciona o ficheiro e carrega em **Import Users**.
4. Confirma a mensagem de sucesso e os quatro utilizadores na listagem.

Os identificadores do exemplo devem estar livres na base de dados.

A importação utiliza uma única transação: todos os utilizadores são guardados
no fim. Se uma linha tiver dados inválidos, um identificador repetido no ficheiro
ou um identificador já existente, nenhuma das linhas dessa importação é guardada.
Os utilizadores que já existiam mantêm-se.

Ficheiros sem utilizadores, cabeçalhos inválidos, linhas com um número incorreto
de campos e ficheiros com codificação inválida são rejeitados. Linhas vazias são
ignoradas. Uma segunda importação do mesmo ficheiro é rejeitada por identificadores
já existentes.

## Base de dados

O serviço utiliza exclusivamente o ficheiro SQLite `user_service/users.sqlite`.
O caminho é calculado a partir da localização de `su_app.py`, independentemente
da pasta a partir da qual o serviço é iniciado.

As tabelas inexistentes e os papéis predefinidos são criados no arranque:

| Tabela | Conteúdo |
| --- | --- |
| `users` | `id`, `name` e `password` com hash. |
| `roles` | `id` e `name`, sendo o nome único. |
| `user_roles` | Associação entre `user_id` e `role_id`, com chave primária composta. |

A relação entre utilizadores e papéis é de muitos para muitos, permitindo que
um utilizador tenha vários papéis e que um papel pertença a vários utilizadores.
As chaves estrangeiras são ativadas em cada ligação SQLite, impedindo associações
a utilizadores ou papéis inexistentes e a remoção direta de registos referenciados.

## Testes

```powershell
.\venv\Scripts\python.exe -m unittest discover -s user_service -v
```

Os testes de criação e importação usam uma base de dados temporária.
Também são verificadas as contagens da página inicial, incluindo utilizadores
com vários papéis, a preservação dos campos após erros e os redirecionamentos
de sucesso.
São ainda verificadas as chaves estrangeiras e a rejeição de papéis em falta
na base de dados, incluindo o rollback de toda a importação CSV.

## Estrutura

- [su_app.py](su_app.py): configuração da aplicação, consultas, validação,
  criação de utilizadores, importação CSV, contagens do resumo e rotas.
- [user_data.py](user_data.py): modelos SQLAlchemy `User` e `Role` e a tabela
  de associação `UserRoles`.
- [templates/](templates/): páginas HTML e estrutura comum da aplicação.
- [requirements.txt](requirements.txt): dependências Python do serviço.
- [test_su_app.py](test_su_app.py): testes automatizados do serviço.
- [test_users.csv](test_users.csv): exemplo de importação com cinco utilizadores,
  incluindo um utilizador com vários papéis.
