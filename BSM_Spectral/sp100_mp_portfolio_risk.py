"""
S&P 100 Random Matrix Theory Portfolio Risk Engine

Pipeline:
Prices -> Returns -> Correlation -> Marchenko-Pastur filtering
-> Covariance -> Portfolio Volatility -> VaR/CVaR
-> Asset Risk Attribution -> Eigenfactor Risk -> Stress Tests

Usage:
    python sp100_mp_portfolio_risk.py

Optional command-line arguments:
    --start 2013-01-01
    --end 2019-01-01
    --portfolio-value 1000000
    --confidence 0.95
    --window 252
    --no-plots
"""

import argparse
import random
import warnings

import numpy as np
import pandas as pd
import requests
import yfinance as yf
import matplotlib.pyplot as plt
import seaborn as sns

from scipy.stats import norm


warnings.filterwarnings("ignore")


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.5; rv:126.0) "
    "Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) "
    "Gecko/20100101 Firefox/126.0",
]


def get_sp100():
    """Retrieve current S&P 100 ticker symbols from Wikipedia."""
    url = "https://en.wikipedia.org/wiki/S%26P_100"
    headers = {"User-Agent": random.choice(USER_AGENTS)}

    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()

    tables = pd.read_html(response.text)

    # Wikipedia's table position can change, so locate the table by column name.
    for table in tables:
        if "Symbol" in table.columns:
            tickers = table["Symbol"].astype(str).tolist()
            if len(tickers) >= 80:
                return tickers

    raise RuntimeError("Could not locate the S&P 100 constituent table.")


def download_prices(tickers, start_date, end_date):
    """Download adjusted historical close prices."""
    data = yf.download(
        tickers=tickers,
        start=start_date,
        end=end_date,
        group_by="ticker",
        auto_adjust=True,
        threads=True,
        progress=False,
    )

    if data.empty:
        raise RuntimeError("No market data was downloaded.")

    closes = {}

    for ticker in tickers:
        try:
            series = data[(ticker, "Close")]
            if isinstance(series, pd.Series):
                closes[ticker] = series
        except (KeyError, TypeError):
            continue

    prices = pd.DataFrame(closes)

    # Remove completely unavailable tickers.
    prices = prices.dropna(axis=1, how="all")

    # Remove assets with more than 5% missing observations.
    prices = prices.dropna(axis=1, thresh=int(0.95 * len(prices)))

    # Remaining observations must be complete for this RMT analysis.
    prices = prices.dropna()

    if prices.shape[1] < 10:
        raise RuntimeError(
            "Too few usable securities remain after cleaning."
        )

    return prices


def calculate_returns(prices):
    """Calculate daily log returns."""
    returns = np.log(prices / prices.shift(1)).dropna()
    return returns


def empirical_correlation(returns):
    """Calculate empirical correlation matrix."""
    return returns.corr()


def mp_bounds(n_assets, n_observations, sigma2=1.0):
    """
    Marchenko-Pastur bounds.

    q = N/T for N assets and T observations.
    """
    q = n_assets / n_observations

    lambda_plus = sigma2 * (1.0 + np.sqrt(q)) ** 2
    lambda_minus = sigma2 * (1.0 - np.sqrt(q)) ** 2

    return q, lambda_minus, lambda_plus


def eigendecompose(correlation):
    """Symmetric eigendecomposition, sorted largest first."""
    eigenvalues, eigenvectors = np.linalg.eigh(correlation.values)

    idx = np.argsort(eigenvalues)[::-1]

    eigenvalues = eigenvalues[idx]
    eigenvectors = eigenvectors[:, idx]

    return eigenvalues, eigenvectors


def mp_filter_correlation(
    correlation,
    eigenvalues,
    eigenvectors,
    lambda_plus,
):
    """
    MP eigenvalue clipping.

    Eigenvalues above lambda_plus are retained.
    Eigenvalues inside the MP noise band are replaced by
    their mean. The matrix is subsequently renormalized
    to have unit diagonal.
    """
    noise_mask = eigenvalues <= lambda_plus

    if noise_mask.any():
        noise_mean = eigenvalues[noise_mask].mean()
    else:
        noise_mean = 1.0

    filtered_eigenvalues = eigenvalues.copy()
    filtered_eigenvalues[noise_mask] = noise_mean

    filtered = (
        eigenvectors
        @ np.diag(filtered_eigenvalues)
        @ eigenvectors.T
    )

    filtered = (filtered + filtered.T) / 2.0

    # Normalize back to a correlation matrix.
    diagonal = np.sqrt(np.maximum(np.diag(filtered), 1e-12))

    filtered = filtered / np.outer(diagonal, diagonal)

    np.fill_diagonal(filtered, 1.0)

    filtered = pd.DataFrame(
        filtered,
        index=correlation.index,
        columns=correlation.columns,
    )

    return filtered, filtered_eigenvalues, noise_mask


def correlation_to_covariance(correlation, returns):
    """Convert correlation matrix to annualized covariance matrix."""
    annual_vol = returns.std() * np.sqrt(252.0)

    D = np.diag(annual_vol.values)

    covariance = D @ correlation.values @ D

    return pd.DataFrame(
        covariance,
        index=correlation.index,
        columns=correlation.columns,
    ), annual_vol


def equal_weights(returns):
    """Create an equal-weight portfolio."""
    n = returns.shape[1]
    return pd.Series(
        np.ones(n) / n,
        index=returns.columns,
        name="Weight",
    )


def portfolio_statistics(
    returns,
    weights,
    covariance,
    confidence,
    portfolio_value,
):
    """Calculate portfolio volatility, historical VaR/CVaR."""
    w = weights.values

    portfolio_variance = w.T @ covariance.values @ w
    portfolio_volatility = np.sqrt(max(portfolio_variance, 0.0))

    portfolio_returns = returns @ weights

    historical_var_return = np.quantile(
        portfolio_returns,
        1.0 - confidence,
    )

    tail = portfolio_returns[
        portfolio_returns <= historical_var_return
    ]

    historical_cvar_return = tail.mean()

    z = norm.ppf(confidence)

    parametric_var_return = (
        z * portfolio_volatility / np.sqrt(252.0)
    )

    results = {
        "portfolio_volatility": portfolio_volatility,
        "historical_var_return": -historical_var_return,
        "historical_cvar_return": -historical_cvar_return,
        "parametric_var_return": parametric_var_return,
        "historical_var_dollar": (
            -historical_var_return * portfolio_value
        ),
        "historical_cvar_dollar": (
            -historical_cvar_return * portfolio_value
        ),
        "parametric_var_dollar": (
            parametric_var_return * portfolio_value
        ),
        "portfolio_returns": portfolio_returns,
    }

    return results


def risk_contribution(weights, covariance):
    """
    Calculate marginal and component volatility contribution.
    """
    w = weights.values

    portfolio_volatility = np.sqrt(
        w.T @ covariance.values @ w
    )

    marginal = (
        covariance.values @ w
    ) / portfolio_volatility

    component = w * marginal

    result = pd.DataFrame(
        {
            "Weight": weights,
            "Marginal_Risk": marginal,
            "Component_Risk": component,
        },
        index=weights.index,
    )

    result["Risk_%"] = (
        result["Component_Risk"] /
        portfolio_volatility
    )

    return result.sort_values(
        "Risk_%",
        ascending=False,
    )


def factor_risk(eigenvalues, eigenvectors, weights):
    """Calculate portfolio risk contribution by eigenfactor."""
    w = weights.values

    factor_exposure = eigenvectors.T @ w

    factor_variance = (
        eigenvalues *
        factor_exposure ** 2
    )

    result = pd.DataFrame(
        {
            "Factor": np.arange(1, len(eigenvalues) + 1),
            "Eigenvalue": eigenvalues,
            "Exposure": factor_exposure,
            "Variance_Contribution": factor_variance,
        }
    )

    total = result["Variance_Contribution"].sum()

    if total > 0:
        result["Variance_%"] = (
            result["Variance_Contribution"] / total
        )
    else:
        result["Variance_%"] = 0.0

    return result


def volatility_stress(
    filtered_correlation,
    annual_vol,
    weights,
    multiplier=1.50,
):
    """Calculate portfolio volatility under a volatility shock."""
    stressed_vol = annual_vol * multiplier

    D = np.diag(stressed_vol.values)

    covariance = (
        D
        @ filtered_correlation.values
        @ D
    )

    w = weights.values

    return np.sqrt(
        w.T @ covariance @ w
    )


def plot_eigenvalues(
    eigenvalues,
    lambda_minus,
    lambda_plus,
    save_path=None,
):
    """Plot empirical eigenvalue spectrum and MP bounds."""
    plt.figure(figsize=(12, 6))

    plt.plot(
        range(1, len(eigenvalues) + 1),
        eigenvalues,
        marker="o",
        linewidth=1,
    )

    plt.axhline(
        lambda_plus,
        linestyle="--",
        label=r"$\lambda_+$",
    )

    plt.axhline(
        lambda_minus,
        linestyle="--",
        label=r"$\lambda_-$",
    )

    plt.xlabel("Principal Component")
    plt.ylabel("Eigenvalue")
    plt.title("S&P 100 Eigenvalue Spectrum vs MP Bounds")
    plt.legend()
    plt.tight_layout()

    if save_path:
        plt.savefig(save_path, dpi=150)

    plt.show()


def plot_correlation_comparison(
    raw,
    filtered,
    save_path=None,
):
    """Plot raw and MP-filtered correlation matrices."""
    fig, axes = plt.subplots(
        1,
        2,
        figsize=(18, 7),
    )

    sns.heatmap(
        raw,
        cmap="coolwarm",
        center=0,
        ax=axes[0],
    )

    axes[0].set_title("Empirical Correlation")

    sns.heatmap(
        filtered,
        cmap="coolwarm",
        center=0,
        ax=axes[1],
    )

    axes[1].set_title("MP-Filtered Correlation")

    plt.tight_layout()

    if save_path:
        plt.savefig(save_path, dpi=150)

    plt.show()


def main():
    parser = argparse.ArgumentParser(
        description="S&P 100 MP Portfolio Risk Engine"
    )

    parser.add_argument(
        "--start",
        default="2013-01-01",
    )

    parser.add_argument(
        "--end",
        default="2019-01-01",
    )

    parser.add_argument(
        "--portfolio-value",
        type=float,
        default=1_000_000.0,
    )

    parser.add_argument(
        "--confidence",
        type=float,
        default=0.95,
    )

    parser.add_argument(
        "--no-plots",
        action="store_true",
    )

    args = parser.parse_args()

    print("=" * 70)
    print("S&P 100 RANDOM MATRIX PORTFOLIO RISK ENGINE")
    print("=" * 70)

    print("\nDownloading S&P 100 constituents...")
    tickers = get_sp100()

    print(f"Constituents found: {len(tickers)}")

    print("\nDownloading price history...")
    prices = download_prices(
        tickers,
        args.start,
        args.end,
    )

    returns = calculate_returns(prices)

    N = returns.shape[1]
    T = returns.shape[0]

    print(f"Usable assets:       {N}")
    print(f"Observations:        {T}")

    correlation = empirical_correlation(returns)

    eigenvalues, eigenvectors = eigendecompose(
        correlation
    )

    q, lambda_minus, lambda_plus = mp_bounds(
        N,
        T,
    )

    print("\nMarchenko-Pastur parameters")
    print("-" * 40)
    print(f"q:                   {q:.6f}")
    print(f"Lambda minus:        {lambda_minus:.6f}")
    print(f"Lambda plus:         {lambda_plus:.6f}")

    filtered_corr, filtered_eigenvalues, noise_mask = (
        mp_filter_correlation(
            correlation,
            eigenvalues,
            eigenvectors,
            lambda_plus,
        )
    )

    signal_count = int((~noise_mask).sum())
    noise_count = int(noise_mask.sum())

    print(f"Signal eigenvalues:   {signal_count}")
    print(f"Noise eigenvalues:    {noise_count}")

    # Covariance matrices.
    raw_covariance, annual_vol = correlation_to_covariance(
        correlation,
        returns,
    )

    filtered_covariance, _ = correlation_to_covariance(
        filtered_corr,
        returns,
    )

    # Equal-weight portfolio.
    weights = equal_weights(returns)

    raw_portfolio_vol = np.sqrt(
        weights.values.T
        @ raw_covariance.values
        @ weights.values
    )

    filtered_stats = portfolio_statistics(
        returns,
        weights,
        filtered_covariance,
        args.confidence,
        args.portfolio_value,
    )

    print("\nPORTFOLIO RISK")
    print("=" * 70)
    print(f"Portfolio value:          ${args.portfolio_value:,.2f}")
    print(f"Raw annual volatility:    {raw_portfolio_vol:.2%}")
    print(
        f"MP annual volatility:     "
        f"{filtered_stats['portfolio_volatility']:.2%}"
    )

    print(
        f"Historical {args.confidence:.0%} 1-day VaR: "
        f"{filtered_stats['historical_var_return']:.2%}"
    )

    print(
        f"Historical {args.confidence:.0%} 1-day VaR: "
        f"${filtered_stats['historical_var_dollar']:,.2f}"
    )

    print(
        f"Historical {args.confidence:.0%} 1-day CVaR: "
        f"{filtered_stats['historical_cvar_return']:.2%}"
    )

    print(
        f"Historical {args.confidence:.0%} 1-day CVaR: "
        f"${filtered_stats['historical_cvar_dollar']:,.2f}"
    )

    print(
        f"Parametric {args.confidence:.0%} 1-day VaR: "
        f"{filtered_stats['parametric_var_return']:.2%}"
    )

    print(
        f"Parametric {args.confidence:.0%} 1-day VaR: "
        f"${filtered_stats['parametric_var_dollar']:,.2f}"
    )

    # Asset-level risk contribution.
    risk_table = risk_contribution(
        weights,
        filtered_covariance,
    )

    # Factor-level risk contribution.
    factor_table = factor_risk(
        filtered_eigenvalues,
        eigenvectors,
        weights,
    )

    # Stress test.
    stress_vol = volatility_stress(
        filtered_corr,
        annual_vol,
        weights,
        multiplier=1.50,
    )

    print("\nRISK ATTRIBUTION")
    print("=" * 70)
    print(risk_table.head(15).to_string())

    print("\nTOP EIGENFACTORS")
    print("=" * 70)
    print(
        factor_table
        .head(15)
        .to_string(index=False)
    )

    print("\nSTRESS TEST")
    print("=" * 70)
    print(
        f"50% volatility shock: "
        f"{stress_vol:.2%} annualized portfolio volatility"
    )

    # Save analytical outputs.
    risk_table.to_csv(
        "asset_risk_contribution.csv"
    )

    factor_table.to_csv(
        "factor_risk_contribution.csv",
        index=False,
    )

    filtered_corr.to_csv(
        "mp_filtered_correlation.csv"
    )

    filtered_covariance.to_csv(
        "mp_filtered_covariance.csv"
    )

    pd.DataFrame(
        {
            "Eigenvalue": eigenvalues,
            "Filtered_Eigenvalue": filtered_eigenvalues,
            "MP_Noise": noise_mask,
        }
    ).to_csv(
        "mp_eigenvalues.csv",
        index=False,
    )

    print("\nFiles written:")
    print("  asset_risk_contribution.csv")
    print("  factor_risk_contribution.csv")
    print("  mp_filtered_correlation.csv")
    print("  mp_filtered_covariance.csv")
    print("  mp_eigenvalues.csv")

    if not args.no_plots:
        plot_eigenvalues(
            eigenvalues,
            lambda_minus,
            lambda_plus,
        )

        plot_correlation_comparison(
            correlation,
            filtered_corr,
        )


if __name__ == "__main__":
    main()
