# Simple Portfolio Tracker
### Technical Specificaiton

### Overview
The goal of the simple portfolio tracker is to show how a set of portfolio transactions performed against the S&P 500 and the NASDAQ. To achieve this, we will create shadow portfolios for VOO and QQQ. For each portfolio investment, we will assume the same dollar amount was invested in VOO and QQQ. 

For example, we enter, into the main portfolio, the following transaction: [Buy, MSFT, 100 shares at $100]. The total value of that transaction is $10,000. Thus, we would record an investment of $10,000 in the VOO portfolio by finding the prices of VOO on the same date and then calculating the number of shares purchased. Assuming the price of VOO was $25 when the MSFT purhase was made, we would record a purchase of 400 VOO at $25. 

The portfolio will be stored in a table with the following headings.
1. DATE: The date of the transaction.
2. TICKER: The ticker of the security. We will track individual stocks, mutual funds, ETFs and crypto.
3. PURCHASE PRICE: The price paid per share.
4. SHARES PURCHASED: The quantity of shares. Note: it is expected most transactions, in both the main portfolio and the shadow portfolios will be in fractional shares. The application needs to handle fractional shares to five decimal points.
5. TOTAL VALUE: The total value of the transaction (PURCHASE PRICE * SHARES PURCHASED).

The two shadow portfolios, for QQQ and VOO, will have the same structure.

Sprint 1 Goal: For sprint 1 we will implement the following user story.

### User Story 1: As a user, I want to be able to enter transactions by editing a text file and then I want the app to show a web page displaying the full table of transactions in my portfolio and the shadow portfolios.

### Non-Functional Requirements
1. We want to get security prices from a fee API such as yfinance.
2. In these early sprints, we can just run the web page on the local host. However, eventually this will be an app that needs to be available 24/7 from any device. We will want to host on a site that enables us to run for free or very low cost (<$5 per month).
3. Stability and robustness are critical. A full suite of unit tests and functionl tests must be created as we implement each user story. We cannot move to a new user story until the full test suite is passing. This is critical to enable us to continue enhancing the applications while remaining highly available to users. 