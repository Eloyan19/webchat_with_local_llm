# Примеры вопросов для RAG-режима

Вопросы, на которые сервис (**https://llm.jorchik.com**, RAG включён) отвечает с источником.
Это не теория — все пункты ниже реально ответились с цитатой в полном прогоне оптимизированного
конфига (см. [`eval/REPORT-optimization.md`](eval/REPORT-optimization.md)).

Корпус индекса — **compose-samples** (демо-приложения Google: Jetchat, JetNews, Jetsnack,
Jetcaster, Reply + README репозитория). Вопросы должны быть про эти приложения.

## Отвечает ✅

**Jetchat**
- In Jetchat's ConversationUiState, what does the addMessage(msg) function do — at which position in the list is a new message inserted? → `ConversationUiState.kt`
- What constructor parameters does Jetchat's ConversationUiState class take? → `ConversationUiState.kt`
- Which UI tests are included in the Jetchat androidTest suite, and what does each one cover? → `README.md`

**JetNews**
- In JetNews's HomeViewModel, HomeUiState is a sealed interface. Which three common properties does it declare? → `HomeViewModel.kt`
- What fields does the ErrorMessage data class in JetNews contain? → `ErrorMessage.kt`

**Jetsnack**
- In Jetsnack's JetsnackNavController, what does the upPress() function do, and what annotation is the class marked with? → `JetsnackNavController.kt`
- In Jetsnack, what kind of Kotlin declaration is SnackbarManager, and how does it expose its messages? → `SnackbarManager.kt`

**Jetcaster / репозиторий**
- What are the three main screens/components of the Jetcaster phone app? → `README.md`
- What copyright year is stated in the top-level README license header of the Jetpack Compose Samples repository? → `README.md`

## Рецепт хорошего вопроса
Работает, когда вопрос про **конкретную сущность в названном демо-приложении**:
- назови **приложение** (Jetchat / JetNews / Jetsnack / Jetcaster / Reply);
- назови **класс / функцию / поле / фичу** (`ConversationUiState`, `HomeViewModel`, `SnackbarManager`, `upPress()` …);
- спрашивай про **что делает / какие параметры / какие поля / что возвращает**.

## Что НЕ сработает (честное «не знаю»)
- **API самого фреймворка** («параметры `Column`», «как работает `remember`») — этого нет в корпусе (индексированы приложения, а не `androidx.compose`).
- **Абстрактные/оценочные** вопросы («какой код лучше?») — нечего процитировать.
- **Вопросы-продолжения** («а что он возвращает?») без предыдущего контекста — местоимение не к чему привязать.

> Это by design: без источника в корпусе сервис не отвечает из общих знаний модели, а честно
> абстейнит — анти-галлюцинационный гейт. Хочешь вопросы по API Compose — надо доиндексировать
> `androidx.compose` в `../rag`.
